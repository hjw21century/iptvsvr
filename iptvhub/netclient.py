"""极简 HTTP 客户端（仅依赖标准库）。

负责：上游清单抓取、流媒体探测时的限时/限量读取、主机并发限流、IP 版本识别。
"""

import gzip
import io
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Dict, Optional, Tuple

_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE
# 大量上游 IPTV 服务器使用老旧 TLS 栈，放宽握手要求，否则会被误判为"不可用"
try:
    _UNVERIFIED.set_ciphers("DEFAULT@SECLEVEL=1")
except ssl.SSLError:  # pragma: no cover - 取决于 OpenSSL 编译选项
    pass


def encode_url(url: str) -> str:
    """把含中文/空格的 URL 转成合法的 ASCII 形式。

    http.client 只接受 ASCII，直接请求会抛 UnicodeEncodeError——实测上游清单里
    约 1/6 的链接路径带非 ASCII 字符，不处理会被整批误判为不可用。
    """
    if all(ord(ch) < 128 for ch in url) and " " not in url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url

    netloc = parts.netloc
    if any(ord(ch) > 127 for ch in netloc):
        host = parts.hostname or ""
        try:
            encoded_host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            encoded_host = urllib.parse.quote(host)
        netloc = encoded_host
        if parts.port:
            netloc = "%s:%d" % (netloc, parts.port)
        if parts.username:
            auth = parts.username + ((":" + parts.password) if parts.password else "")
            netloc = "%s@%s" % (urllib.parse.quote(auth, safe=":"), netloc)

    # safe 里保留 % ，避免把已经编码好的 %XX 二次编码
    path = urllib.parse.quote(parts.path, safe="/%:@&=+$,~!*'()")
    query = urllib.parse.quote(parts.query, safe="/%:@&=+$,?~!*'()[]|")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


class HostLimiter:
    """按主机限制并发，避免把某个上游打挂（也能降低被封禁概率）。"""

    def __init__(self, per_host: int):
        self.per_host = max(1, int(per_host))
        self._lock = threading.Lock()
        self._sems: Dict[str, threading.Semaphore] = {}

    def acquire(self, host: str) -> threading.Semaphore:
        with self._lock:
            sem = self._sems.get(host)
            if sem is None:
                sem = threading.Semaphore(self.per_host)
                self._sems[host] = sem
        sem.acquire()
        return sem


class HttpClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.user_agent = cfg.get("user_agent", "Mozilla/5.0")
        self.limiter = HostLimiter(cfg.get("per_host_concurrency", 4))
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_UNVERIFIED),
            urllib.request.HTTPRedirectHandler(),
        )
        self._ipver_cache: Dict[str, int] = {}
        self._ipver_lock = threading.Lock()

    # ----------------------------------------------------------------- 基础
    def _request(self, url: str, headers: Optional[dict] = None, referer: Optional[str] = None):
        base = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Connection": "close",
        }
        if referer:
            base["Referer"] = referer
        if headers:
            base.update(headers)
        return urllib.request.Request(encode_url(url), headers=base)

    def open(self, url: str, timeout: float = None, headers: dict = None, referer: str = None):
        """打开连接，返回 (response, ttfb_seconds)。调用方负责 close()。"""
        timeout = timeout or self.cfg.get("connect_timeout", 6)
        started = time.time()
        response = self._opener.open(self._request(url, headers, referer), timeout=timeout)
        return response, time.time() - started

    @staticmethod
    def _decode_body(raw: bytes, response) -> bytes:
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        try:
            if "gzip" in encoding:
                return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            if "deflate" in encoding:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            return raw
        return raw

    def fetch_bytes(self, url: str, timeout: float = None, max_bytes: int = None,
                    headers: dict = None, referer: str = None) -> Tuple[int, bytes]:
        """抓取文本类资源（清单 / m3u8），返回 (status, body)。"""
        timeout = timeout or self.cfg.get("fetch_timeout", 15)
        max_bytes = max_bytes or self.cfg.get("fetch_max_bytes", 8 << 20)
        hdrs = {"Accept-Encoding": "gzip, deflate"}
        if headers:
            hdrs.update(headers)
        response = None
        try:
            response, _ = self.open(url, timeout=timeout, headers=hdrs, referer=referer)
            raw = response.read(max_bytes + 1)
            return response.getcode() or 200, self._decode_body(raw, response)
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except Exception:
            # DNS 失效、连接超时、TLS 失败等：统一当作"该源本轮不可用"
            return 0, b""
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def fetch_text(self, url: str, timeout: float = None, max_bytes: int = None,
                   referer: str = None) -> Tuple[int, str]:
        status, body = self.fetch_bytes(url, timeout=timeout, max_bytes=max_bytes, referer=referer)
        if not body:
            return status, ""
        for encoding in ("utf-8", "gb18030"):
            try:
                return status, body.decode(encoding)
            except UnicodeDecodeError:
                continue
        return status, body.decode("utf-8", errors="ignore")

    # ------------------------------------------------------------ 流式读取
    @staticmethod
    def read_limited(response, max_bytes: int, deadline: float, chunk: int = 32768) -> bytes:
        """在字节数和截止时间双重约束下读取码流，用于测速。"""
        buffer = bytearray()
        while len(buffer) < max_bytes and time.time() < deadline:
            try:
                piece = response.read(min(chunk, max_bytes - len(buffer)))
            except Exception:
                break
            if not piece:
                break
            buffer.extend(piece)
        return bytes(buffer)

    # -------------------------------------------------------------- 工具
    @staticmethod
    def host_of(url: str) -> str:
        try:
            return (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            return ""

    def ip_version(self, host: str) -> int:
        """返回 4 / 6 / 0(未知)。用于导出时按 IPv4、IPv6 过滤。"""
        if not host:
            return 0
        with self._ipver_lock:
            cached = self._ipver_cache.get(host)
        if cached is not None:
            return cached

        version = 0
        try:
            socket.inet_aton(host)
            version = 4
        except OSError:
            if ":" in host:
                version = 6
            else:
                try:
                    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
                    families = {info[0] for info in infos}
                    if socket.AF_INET in families:
                        version = 4
                    elif socket.AF_INET6 in families:
                        version = 6
                except Exception:
                    version = 0

        with self._ipver_lock:
            self._ipver_cache[host] = version
        return version
