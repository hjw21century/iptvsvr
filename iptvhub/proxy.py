"""HLS 中转代理。

浏览器里放不出来、VLC 却正常，是两条浏览器独有的限制造成的：

  1. 混合内容：页面是 https://，而绝大多数直播源是 http://，
     hls.js 发起的 XHR 会被直接拦掉（表现为 manifestLoadError）。
  2. 跨域：上游几乎都不返回 Access-Control-Allow-Origin。

所以把流经本站 HTTPS 中转一道，并由我们补上 CORS 头。为避免变成公开代理：
首个 manifest 必须是库里已知的地址，manifest 内部的分片/子清单地址在改写时
用 HMAC 签名，只有我们签发过的才放行。
"""

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import defaultdict
from hashlib import sha256
from typing import Any, Dict, Optional, Tuple

from .netclient import HttpClient

log = logging.getLogger("iptvhub.proxy")

_URI_ATTR_RE = re.compile(r'(URI=")([^"]+)(")')
MANIFEST_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/x-mpegurl")


class Meter:
    """中转流量计量与配额。

    中转是流量放大器：一个人在网页看一小时 1080p，出站就是 1.5GB 左右。
    所以按"全站每日 / 单访客每日 / 单次请求"三层设闸，超了就让用户改用
    VLC 直连（那条路不经过本站，一点带宽都不占）。
    """

    def __init__(self, cfg: dict, store=None):
        self.store = store
        self.daily_limit = int(float(cfg.get("proxy_daily_gb", 10)) * (1 << 30))
        self.per_ip_daily = int(float(cfg.get("proxy_per_ip_daily_mb", 1200)) * (1 << 20))
        self.per_ip_concurrent = int(cfg.get("proxy_per_ip_concurrent", 2))
        self.max_request_bytes = int(float(cfg.get("proxy_max_request_mb", 200)) * (1 << 20))
        self.max_request_seconds = float(cfg.get("proxy_max_request_seconds", 300))

        self._lock = threading.Lock()
        self._day = self._today()
        self._total = 0
        self._per_ip: Dict[str, int] = defaultdict(int)
        self._active: Dict[str, int] = defaultdict(int)
        self._blocked = 0
        self._requests = 0
        self._unsaved = 0
        self._load()

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y%m%d")

    @property
    def _meta_key(self) -> str:
        return "proxy_usage_%s" % self._day

    def _load(self) -> None:
        if not self.store:
            return
        try:
            saved = json.loads(self.store.get_meta(self._meta_key, "") or "{}")
            self._total = int(saved.get("bytes", 0))
            self._requests = int(saved.get("requests", 0))
            self._blocked = int(saved.get("blocked", 0))
            self._per_ip.update({k: int(v) for k, v in (saved.get("clients") or {}).items()})
        except Exception:  # noqa: BLE001 - 计量不是关键路径，读不出来就从零开始
            pass

    def _save(self, force: bool = False) -> None:
        if not self.store:
            return
        if not force and self._unsaved < (32 << 20):   # 每 32MB 落一次盘，避免频繁写
            return
        self._unsaved = 0
        payload = {
            "bytes": self._total, "requests": self._requests, "blocked": self._blocked,
            # 只保留用量最大的 200 个访客，避免 meta 无限膨胀
            "clients": dict(sorted(self._per_ip.items(), key=lambda kv: -kv[1])[:200]),
        }
        try:
            self.store.set_meta(self._meta_key, json.dumps(payload))
        except Exception:  # noqa: BLE001
            pass

    def _rollover(self) -> None:
        today = self._today()
        if today != self._day:
            self._save(force=True)
            self._day = today
            self._total = 0
            self._requests = 0
            self._blocked = 0
            self._per_ip.clear()
            self._load()

    def check(self, client: str) -> Tuple[bool, str]:
        with self._lock:
            self._rollover()
            if self.daily_limit and self._total >= self.daily_limit:
                self._blocked += 1
                return False, "本站今日网页预览流量已用完，请复制地址用 VLC 等播放器观看"
            if self.per_ip_daily and self._per_ip[client] >= self.per_ip_daily:
                self._blocked += 1
                return False, "你的网页预览用量已达今日上限，请复制地址用 VLC 等播放器观看"
            if self.per_ip_concurrent and self._active[client] >= self.per_ip_concurrent:
                self._blocked += 1
                return False, "同时只能预览 %d 路，请先关闭其它播放" % self.per_ip_concurrent
            self._active[client] += 1
            self._requests += 1
            return True, ""

    def done(self, client: str) -> None:
        with self._lock:
            self._active[client] = max(0, self._active[client] - 1)

    def add(self, client: str, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self._total += count
            self._per_ip[client] += count
            self._unsaved += count
            self._save()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._rollover()
            active = sum(self._active.values())
            return {
                "day": self._day,
                "bytes": self._total,
                "gb": round(self._total / float(1 << 30), 3),
                "daily_limit_gb": round(self.daily_limit / float(1 << 30), 2),
                "percent": round(100.0 * self._total / self.daily_limit, 1)
                if self.daily_limit else 0.0,
                "requests": self._requests,
                "blocked": self._blocked,
                "active": active,
                "clients": len(self._per_ip),
                "top_clients": [
                    {"client": k, "mb": round(v / float(1 << 20), 1)}
                    for k, v in sorted(self._per_ip.items(), key=lambda kv: -kv[1])[:5]
                ],
            }

    def flush(self) -> None:
        with self._lock:
            self._save(force=True)


class StreamProxy:
    def __init__(self, cfg: dict, store, client: Optional[HttpClient] = None):
        self.cfg = cfg
        self.store = store
        self.client = client or HttpClient(cfg)
        self.enabled = bool(cfg.get("proxy_enabled", True))
        self.max_concurrent = int(cfg.get("proxy_max_concurrent", 6))
        self.meter = Meter(cfg, store)
        self.manifest_max_bytes = int(cfg.get("manifest_max_bytes", 1 << 20))
        self.chunk_size = 64 * 1024
        self._slots = threading.Semaphore(self.max_concurrent)
        self._secret: Optional[bytes] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 签名
    @property
    def secret_path(self) -> str:
        return os.path.join(self.cfg["paths"]["data"], "proxy_secret")

    def secret(self) -> bytes:
        with self._lock:
            if self._secret:
                return self._secret
            if os.path.exists(self.secret_path):
                with open(self.secret_path, "r", encoding="utf-8") as handle:
                    value = handle.read().strip()
            else:
                value = secrets.token_hex(32)
                with open(self.secret_path, "w", encoding="utf-8") as handle:
                    handle.write(value + "\n")
                os.chmod(self.secret_path, 0o600)
            self._secret = value.encode("utf-8")
            return self._secret

    def sign(self, url: str) -> str:
        return hmac.new(self.secret(), url.encode("utf-8"), sha256).hexdigest()[:16]

    def verify(self, url: str, signature: str) -> bool:
        return bool(signature) and hmac.compare_digest(self.sign(url), signature)

    def proxy_url(self, url: str, signed: bool = True) -> str:
        query = {"u": url}
        if signed:
            query["s"] = self.sign(url)
        return "/proxy?" + urllib.parse.urlencode(query)

    # ------------------------------------------------------------ 访问控制
    def authorized(self, url: str, signature: str) -> bool:
        if self.verify(url, signature):
            return True
        # 未签名时只允许库里已收录的地址，避免成为开放代理
        return bool(self.store and self.store.stream(url))

    # ------------------------------------------------------------ manifest
    def rewrite_manifest(self, text: str, base_url: str) -> str:
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                lines.append(line)
                continue
            if stripped.startswith("#"):
                # #EXT-X-KEY / #EXT-X-MAP 里的 URI 也要一起改写
                lines.append(_URI_ATTR_RE.sub(
                    lambda m: m.group(1) + self.proxy_url(
                        urllib.parse.urljoin(base_url, m.group(2))) + m.group(3),
                    line))
                continue
            absolute = urllib.parse.urljoin(base_url, stripped)
            if absolute.lower().startswith(("http://", "https://")):
                lines.append(self.proxy_url(absolute))
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    @staticmethod
    def looks_like_manifest(head: bytes, content_type: str) -> bool:
        if head.lstrip()[:7].upper() == b"#EXTM3U":
            return True
        lowered = (content_type or "").lower()
        return any(token in lowered for token in ("mpegurl", "m3u8"))

    # ---------------------------------------------------------------- 取流
    def open_upstream(self, url: str, headers: Optional[dict] = None) -> Tuple[object, str, bytes]:
        """返回 (response, 最终URL, 已读取的首包)。"""
        response, _ = self.client.open(url, timeout=self.cfg.get("connect_timeout", 6),
                                       headers=headers)
        final_url = response.geturl() or url
        head = response.read(8192)
        return response, final_url, head

    def acquire(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        try:
            self._slots.release()
        except ValueError:  # pragma: no cover
            pass
