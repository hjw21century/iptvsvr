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
import logging
import os
import re
import secrets
import threading
import urllib.parse
from hashlib import sha256
from typing import Optional, Tuple

from .netclient import HttpClient

log = logging.getLogger("iptvhub.proxy")

_URI_ATTR_RE = re.compile(r'(URI=")([^"]+)(")')
MANIFEST_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/x-mpegurl")


class StreamProxy:
    def __init__(self, cfg: dict, store, client: Optional[HttpClient] = None):
        self.cfg = cfg
        self.store = store
        self.client = client or HttpClient(cfg)
        self.enabled = bool(cfg.get("proxy_enabled", True))
        self.max_concurrent = int(cfg.get("proxy_max_concurrent", 12))
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
