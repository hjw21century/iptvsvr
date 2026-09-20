"""采集 -> 探测 -> 评分 -> 导出 的完整流水线。"""

import logging
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from . import export, rank
from .classify import Classifier
from .config import load_config, load_groups, load_sources
from .netclient import HttpClient
from .parser import NoiseFilter, parse_playlist
from .probe import Prober
from .store import Store
from .util import fmt_duration, human_time

log = logging.getLogger("iptvhub.pipeline")


class Pipeline:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or load_config()
        self.groups_cfg = load_groups()
        self.classifier = Classifier(self.groups_cfg)
        self.noise = NoiseFilter(self.groups_cfg.get("blocked_keywords", []))
        self.client = HttpClient(self.cfg)
        self.prober = Prober(self.client, self.cfg)
        self.store = Store(self.cfg["paths"]["db"])

    # --------------------------------------------------------------- 采集
    def collect(self, sources: Optional[List[dict]] = None) -> Dict[str, Any]:
        sources = sources if sources is not None else load_sources()
        collected: Dict[str, Dict[str, Any]] = {}
        ok_sources = 0

        def fetch(source):
            status, text = self.client.fetch_text(source["url"])
            return source, status, text

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(sources)))) as pool:
            futures = [pool.submit(fetch, source) for source in sources]
            for future in as_completed(futures):
                try:
                    source, status, text = future.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("源抓取异常: %s", exc)
                    continue
                if status != 200 or not text.strip():
                    log.warning("源不可用 [%s] status=%s", source["name"], status)
                    continue

                entries = parse_playlist(text, source["type"], source["url"],
                                         source["name"], source["weight"], self.noise)
                if not entries:
                    log.warning("源解析为空 [%s]", source["name"])
                    continue

                ok_sources += 1
                log.info("源 %-16s 条目 %5d", source["name"], len(entries))
                for entry in entries:
                    self._merge_entry(collected, entry)

        return {"candidates": collected, "sources_ok": ok_sources, "sources_total": len(sources)}

    def _merge_entry(self, collected: Dict[str, Dict[str, Any]], entry) -> None:
        display, key = self.classifier.canonical(entry.name)
        if not key or not display:
            return
        group = self.classifier.group_of(display, entry.group)

        parts = urllib.parse.urlsplit(entry.url)
        host = (parts.hostname or "").lower()
        if not host:
            return

        record = collected.get(entry.url)
        if record is None:
            collected[entry.url] = {
                "url": entry.url,
                "channel_key": key,
                "display_name": display,
                "group_title": group,
                "logo": entry.logo or "",
                "host": host,
                "scheme": (parts.scheme or "http").lower(),
                "ip_version": 0,
                "sources": {entry.source} if entry.source else set(),
                "source_weight": entry.weight,
            }
            return

        # 同一 URL 被多个源收录：合并来源、补全台标、取较高权重
        if entry.source:
            record["sources"].add(entry.source)
        if entry.logo and not record["logo"]:
            record["logo"] = entry.logo
        record["source_weight"] = max(record["source_weight"], entry.weight)
        if record["group_title"] == "其他频道" and group != "其他频道":
            record["group_title"] = group

    def _resolve_ip_versions(self, candidates: Dict[str, Dict[str, Any]]) -> None:
        hosts = {record["host"] for record in candidates.values() if record["host"]}
        if not hosts:
            return
        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(self.client.ip_version, hosts))
        for record in candidates.values():
            record["ip_version"] = self.client.ip_version(record["host"])

    # --------------------------------------------------------------- 探测
    def probe_urls(self, urls: List[str], progress_every: int = 200) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if not urls:
            return results

        self.prober.reset_circuits()
        total = len(urls)
        started = time.time()
        done = alive = 0
        workers = max(1, int(self.cfg.get("concurrency", 48)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.prober.probe, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    probe = future.result()
                except Exception as exc:  # noqa: BLE001
                    log.debug("探测异常 %s: %s", url, exc)
                    continue
                results.append({"url": url, "probe": probe})
                done += 1
                alive += 1 if probe.ok else 0
                if progress_every and done % progress_every == 0:
                    elapsed = time.time() - started
                    rate = done / max(elapsed, 1e-6)
                    eta = (total - done) / max(rate, 1e-6)
                    log.info("探测进度 %d/%d 可用 %d (%.1f/s, 剩余 %s)",
                             done, total, alive, rate, fmt_duration(eta))

        log.info("探测完成 %d 条，可用 %d 条，耗时 %s",
                 total, alive, fmt_duration(time.time() - started))
        return results

    # ------------------------------------------------------------- 全流程
    def run(self, limit: int = 0, skip_probe: bool = False,
            recheck_all: bool = False) -> Dict[str, Any]:
        run_id = self.store.start_run()
        started = time.time()
        log.info("=== 开始更新 %s ===", human_time())

        collected = self.collect()
        candidates = collected["candidates"]
        log.info("采集到候选源 %d 条（来自 %d/%d 个上游）",
                 len(candidates), collected["sources_ok"], collected["sources_total"])

        self._resolve_ip_versions(candidates)
        self.store.upsert_candidates(candidates.values())

        # 探测集合 = 本轮候选 + 库里仍在观察期的历史源
        urls = set(candidates.keys())
        for row in self.store.all_streams():
            urls.add(row["url"])

        if not recheck_all:
            cooldown = self.store.cooldown_urls(int(self.cfg.get("recheck_dead_after_hours", 12)))
            skipped = urls & cooldown
            urls -= cooldown
            if skipped:
                log.info("跳过冷却期内的失效源 %d 条", len(skipped))

        url_list = sorted(urls)
        if limit:
            url_list = url_list[:limit]

        probe_results: List[Dict[str, Any]] = []
        if not skip_probe:
            probe_results = self.probe_urls(url_list)
            self.store.record_probes(probe_results, alpha=float(self.cfg.get("ewma_alpha", 0.4)))

        channels = self.rescore_and_export()
        alive = sum(1 for item in probe_results if item["probe"].ok)

        self.store.finish_run(
            run_id,
            sources_ok=collected["sources_ok"], sources_total=collected["sources_total"],
            candidates=len(candidates), probed=len(probe_results),
            alive=alive, channels=len(channels),
            note="耗时 %s" % fmt_duration(time.time() - started),
        )
        pruned = self.store.prune(int(self.cfg.get("prune_after_days", 7)),
                                  int(self.cfg.get("prune_fail_streak", 8)))
        self.store.set_meta("last_run_at", str(int(time.time())))

        log.info("=== 更新完成：频道 %d 个 / 可用源 %d 条 / 清理 %d 条 / 总耗时 %s ===",
                 len(channels), alive, pruned["streams_removed"],
                 fmt_duration(time.time() - started))

        return {
            "run_id": run_id,
            "candidates": len(candidates),
            "probed": len(probe_results),
            "alive": alive,
            "channels": len(channels),
            "pruned": pruned,
            "elapsed": time.time() - started,
        }

    def rescore_and_export(self) -> List[Dict[str, Any]]:
        rows = [dict(row) for row in self.store.all_streams()]
        scores = rank.score_all(rows, self.cfg.get("weights", {}),
                                float(self.cfg.get("ewma_alpha", 0.4)))
        self.store.update_scores(scores)
        for row in rows:
            row["score"] = scores.get(row["url"], 0.0)

        channels = rank.pick_best(rows, self.cfg, self.classifier)
        stats = self.store.stats()
        export.write_outputs(self.cfg["paths"]["data"], channels, stats,
                             epg_url=self.cfg.get("epg_url", ""),
                             site_url=self.cfg.get("site_url", ""))
        return channels
