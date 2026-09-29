"""管理后台 API。

所有接口都要求管理令牌（`X-Admin-Token` 头或 `?token=`）。令牌来源：
`config.json` 的 `server.admin_token`；未设置时自动生成并保存到 `data/admin_token`，
启动日志里会打印，也可用 `python3 -m iptvhub token` 查看。
"""

import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config as config_module
from .classify import Classifier
from .netclient import HttpClient
from .parser import NoiseFilter, parse_playlist
from .probe import Prober
from .runtime import LOG_RING, RUN_STATE
from .util import human_time

log = logging.getLogger("iptvhub.admin")

# 允许后台修改的配置项（其余键只读，避免把服务改挂）
EDITABLE_NUMBERS = {
    "concurrency": (1, 512), "per_host_concurrency": (1, 64),
    "connect_timeout": (1, 60), "probe_timeout": (2, 120),
    "probe_bytes": (4096, 8 << 20), "probe_seconds": (0.5, 30),
    "min_bytes": (512, 4 << 20), "manifest_max_bytes": (4096, 16 << 20),
    "probe_retries": (0, 3), "host_failure_limit": (0, 100),
    "ewma_alpha": (0.05, 1.0), "prune_after_days": (1, 90),
    "prune_fail_streak": (1, 100), "recheck_dead_after_hours": (0, 168),
    "max_backups_per_channel": (0, 10), "max_per_host_per_channel": (1, 10),
    "min_score": (0.0, 1.0), "fetch_timeout": (2, 120),
    "proxy_max_concurrent": (0, 64), "proxy_daily_gb": (0, 2000),
    "proxy_per_ip_daily_mb": (0, 200000), "proxy_per_ip_concurrent": (1, 10),
    "proxy_max_request_mb": (1, 4096), "proxy_max_request_seconds": (10, 3600),
}
EDITABLE_STRINGS = ("epg_url", "site_url", "user_agent")
EDITABLE_WEIGHTS = ("stability", "speed", "quality", "latency", "https_bonus",
                    "ipv4_bonus", "direct_bonus")
EDITABLE_SERVER = {"auto_update": bool, "update_interval_hours": int, "admin_token": str}


class AdminApi:
    def __init__(self, cfg: dict, store, updater, cache, proxy=None):
        self.cfg = cfg
        self.store = store
        self.updater = updater
        self.cache = cache
        self.proxy = proxy
        self.feedback = None          # 由 serve() 注入
        self._token: Optional[str] = None

    # ------------------------------------------------------------------ 鉴权
    @property
    def token_path(self) -> str:
        return os.path.join(self.cfg["paths"]["data"], "admin_token")

    def token(self) -> str:
        configured = (self.cfg.get("server", {}).get("admin_token") or "").strip()
        if configured:
            return configured
        if self._token:
            return self._token
        if os.path.exists(self.token_path):
            with open(self.token_path, "r", encoding="utf-8") as handle:
                self._token = handle.read().strip()
        if not self._token:
            self._token = secrets.token_hex(16)
            with open(self.token_path, "w", encoding="utf-8") as handle:
                handle.write(self._token + "\n")
            os.chmod(self.token_path, 0o600)
            log.info("已生成管理令牌并保存到 %s", self.token_path)
        return self._token

    def authorized(self, supplied: str) -> bool:
        return bool(supplied) and hmac.compare_digest(str(supplied), self.token())

    # ------------------------------------------------------------------ 分发
    def handle(self, method: str, path: str, query: Dict[str, List[str]],
               body: Dict[str, Any]) -> Tuple[int, Any]:
        route = "%s %s" % (method.upper(), path)
        handlers = {
            "GET /summary": self._summary,
            "GET /progress": self._progress,
            "GET /sources": self._sources,
            "GET /config": self._config,
            "GET /streams": self._streams,
            "GET /feedback": self._feedback,
            "GET /history": self._history,
            "POST /update": self._update,
            "POST /export": self._export,
            "POST /prune": self._prune,
            "POST /sources": self._save_sources,
            "POST /sources/test": self._test_source,
            "POST /config": self._save_config,
            "POST /probe": self._probe,
            "POST /stream/delete": self._delete_stream,
            "POST /feedback/hide": self._hide_feedback,
            "POST /feedback/delete": self._delete_feedback,
        }
        handler = handlers.get(route)
        if handler is None:
            return 404, {"error": "not found", "route": route}
        return handler(query, body)

    # -------------------------------------------------------------- 只读接口
    def _summary(self, query, body):
        stats = self.store.stats()
        payload = self.cache.get()
        sources = config_module.load_sources_raw()
        contributions = self.store.source_stats()
        for item in sources:
            item["stats"] = contributions.get(item["name"], {"candidates": 0, "alive": 0})
        return 200, {
            "stats": stats,
            "generated_at": payload.get("generated_at", ""),
            "channel_count": len(payload.get("channels", [])),
            "run": RUN_STATE.snapshot(),
            "runs": self.store.recent_runs(12),
            "errors": self.store.error_breakdown(12),
            "sources": sources,
            "groups": sorted(
                stats.get("groups", []), key=lambda g: -g.get("channels", 0))[:60],
            "server_time": human_time(),
            "auto_update": bool(self.cfg["server"].get("auto_update", True)),
            "update_interval_hours": self.cfg["server"].get("update_interval_hours", 4),
            "data_dir": self.cfg["paths"]["data"],
            "feedback": self.store.feedback_stats(),
            "proxy": self.proxy.meter.snapshot() if self.proxy else None,
        }

    def _progress(self, query, body):
        after = int(float((query.get("after", ["0"])[0]) or 0))
        return 200, {
            "run": RUN_STATE.snapshot(),
            "updating": self.updater.running,
            "logs": LOG_RING.tail(after=after, limit=200),
            "last_seq": LOG_RING.last_seq,
            "generated_at": self.cache.get().get("generated_at", ""),
        }

    def _sources(self, query, body):
        sources = config_module.load_sources_raw()
        contributions = self.store.source_stats()
        for item in sources:
            item["stats"] = contributions.get(item["name"], {"candidates": 0, "alive": 0})
        return 200, {"sources": sources}

    def _config(self, query, body):
        raw = config_module.load_config_file()
        return 200, {
            "config": raw,
            "editable": {
                "numbers": {k: list(v) for k, v in EDITABLE_NUMBERS.items()},
                "strings": list(EDITABLE_STRINGS),
                "weights": list(EDITABLE_WEIGHTS),
                "server": list(EDITABLE_SERVER),
            },
            "path": os.path.join(self.cfg["paths"]["config"], "config.json"),
        }

    def _streams(self, query, body):
        def one(key, default=""):
            return query.get(key, [default])[0]

        alive = one("alive")
        result = self.store.query_streams(
            q=one("q"), group=one("group"),
            alive=int(alive) if alive in ("0", "1") else None,
            error=one("error"), channel_key=one("channel_key"),
            order=one("order", "score"),
            limit=min(int(float(one("limit", "100") or 100)), 500),
            offset=int(float(one("offset", "0") or 0)),
        )
        result["groups"] = self.store.group_names()
        return 200, result

    def _feedback(self, query, body):
        def one(key, default=""):
            return query.get(key, [default])[0]

        data = self.store.list_feedback(
            url=one("url"), channel_key=one("channel"), kind=one("kind"),
            include_hidden=one("hidden") != "0",
            limit=min(int(float(one("limit", "80") or 80)), 300),
            offset=int(float(one("offset", "0") or 0)))
        from .feedback import KINDS, score_adjustment
        summary = self.store.feedback_summary()
        data["kinds"] = KINDS
        data["stats"] = self.store.feedback_stats()
        data["adjustments"] = {url: score_adjustment(counts)
                               for url, counts in summary.items()}
        return 200, data

    def _hide_feedback(self, query, body):
        changed = self.store.set_feedback_hidden(int(body.get("id") or 0),
                                                 bool(body.get("hidden", True)))
        return 200, {"changed": changed}

    def _delete_feedback(self, query, body):
        return 200, {"removed": self.store.delete_feedback(int(body.get("id") or 0))}

    def _history(self, query, body):
        url = query.get("url", [""])[0]
        if not url:
            return 400, {"error": "url required"}
        row = self.store.stream(url)
        return 200, {
            "url": url,
            "stream": dict(row) if row else None,
            "checks": [dict(r) for r in self.store.history(url, 96)],
        }

    # -------------------------------------------------------------- 写入接口
    def _update(self, query, body):
        kwargs = {
            "limit": int(body.get("limit") or 0),
            "skip_probe": bool(body.get("skip_probe")),
            "recheck_all": bool(body.get("recheck_all")),
            "trigger": "admin",
        }
        started = self.updater.trigger(**kwargs)
        return (202 if started else 409), {
            "started": started,
            "running": self.updater.running,
            "options": kwargs,
        }

    def _export(self, query, body):
        from .pipeline import Pipeline
        channels = Pipeline(config_module.load_config()).rescore_and_export()
        return 200, {"channels": len(channels), "message": "已按当前权重重新评分并导出"}

    def _prune(self, query, body):
        removed = self.store.prune(
            int(self.cfg.get("prune_after_days", 7)),
            int(self.cfg.get("prune_fail_streak", 8)))
        return 200, removed

    def _save_sources(self, query, body):
        incoming = body.get("sources")
        if not isinstance(incoming, list):
            return 400, {"error": "sources 必须是数组"}

        cleaned, names = [], set()
        for item in incoming:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            name = str(item.get("name") or "").strip() or url
            if not url.lower().startswith(("http://", "https://")):
                return 400, {"error": "非法地址: %s" % url}
            if name in names:
                return 400, {"error": "名称重复: %s" % name}
            names.add(name)
            kind = str(item.get("type") or "auto").lower()
            if kind not in ("auto", "m3u", "txt"):
                kind = "auto"
            try:
                weight = float(item.get("weight", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            entry = {
                "name": name, "url": url, "type": kind,
                "enabled": bool(item.get("enabled", True)),
                "weight": max(0.1, min(2.0, weight)),
            }
            if item.get("note"):
                entry["note"] = str(item["note"])[:200]
            cleaned.append(entry)

        config_module.save_sources(cleaned)
        return 200, {"saved": len(cleaned),
                     "enabled": sum(1 for s in cleaned if s["enabled"])}

    def _test_source(self, query, body):
        url = str(body.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return 400, {"error": "非法地址"}

        client = HttpClient(self.cfg)
        started = time.time()
        status, text = client.fetch_text(url)
        elapsed = round((time.time() - started) * 1000)
        if status != 200 or not text.strip():
            return 200, {"ok": False, "status": status, "elapsed_ms": elapsed,
                         "entries": 0, "message": "抓取失败或内容为空"}

        groups_cfg = config_module.load_groups()
        noise = NoiseFilter(groups_cfg.get("blocked_keywords", []))
        entries = parse_playlist(text, str(body.get("type") or "auto"), url, "test", 1.0, noise)
        classifier = Classifier(groups_cfg)
        known = {row["url"] for row in self.store.all_streams()}
        samples = []
        for entry in entries[:8]:
            display, _ = classifier.canonical(entry.name)
            samples.append({"name": display, "group": classifier.group_of(display, entry.group),
                            "url": entry.url})
        return 200, {
            "ok": True, "status": status, "elapsed_ms": elapsed,
            "entries": len(entries),
            "new_urls": sum(1 for e in entries if e.url not in known),
            "bytes": len(text),
            "samples": samples,
        }

    def _save_config(self, query, body):
        patch = body.get("config")
        if not isinstance(patch, dict):
            return 400, {"error": "config 必须是对象"}

        raw = config_module.load_config_file()
        applied, rejected = {}, {}

        for key, value in patch.items():
            if key in EDITABLE_NUMBERS:
                low, high = EDITABLE_NUMBERS[key]
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    rejected[key] = "不是数字"
                    continue
                if not low <= number <= high:
                    rejected[key] = "应在 %s ~ %s 之间" % (low, high)
                    continue
                raw[key] = int(number) if float(number).is_integer() and key not in (
                    "probe_seconds", "ewma_alpha", "min_score") else number
                applied[key] = raw[key]
            elif key in EDITABLE_STRINGS:
                raw[key] = str(value)[:400]
                applied[key] = raw[key]
            elif key == "weights" and isinstance(value, dict):
                weights = dict(raw.get("weights") or {})
                for name, weight in value.items():
                    if name not in EDITABLE_WEIGHTS:
                        continue
                    try:
                        weights[name] = max(0.0, min(1.0, float(weight)))
                    except (TypeError, ValueError):
                        rejected["weights.%s" % name] = "不是数字"
                raw["weights"] = weights
                applied["weights"] = weights
            elif key == "server" and isinstance(value, dict):
                server = dict(raw.get("server") or {})
                for name, item in value.items():
                    caster = EDITABLE_SERVER.get(name)
                    if caster is None:
                        rejected["server.%s" % name] = "不可修改"
                        continue
                    try:
                        server[name] = caster(item) if caster is not bool else bool(item)
                    except (TypeError, ValueError):
                        rejected["server.%s" % name] = "类型错误"
                if "update_interval_hours" in server:
                    server["update_interval_hours"] = max(1, min(72, int(
                        server["update_interval_hours"])))
                raw["server"] = server
                applied["server"] = server
            else:
                rejected[key] = "不可修改"

        if applied:
            config_module.save_config_file(raw)
        return 200, {
            "applied": applied, "rejected": rejected,
            "note": "下一轮更新自动生效；server.* 需要重启服务",
        }

    def _probe(self, query, body):
        url = str(body.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return 400, {"error": "非法地址"}
        cfg = config_module.load_config()
        prober = Prober(HttpClient(cfg), cfg)
        result = prober.probe(url)
        return 200, {"url": url, "result": result.as_dict()}

    def _delete_stream(self, query, body):
        url = str(body.get("url") or "").strip()
        if not url:
            return 400, {"error": "url required"}
        removed = self.store.delete_stream(url)
        return 200, {"removed": removed,
                     "note": "若上游仍收录该地址，下轮采集会重新加入"}
