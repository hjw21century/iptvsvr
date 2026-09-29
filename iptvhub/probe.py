"""流媒体深度探测。

与"HTTP 200 即可用"的常见做法不同，这里会真正走一遍播放流程：

    HLS: 拉取 manifest -> 解析 master/variant -> 解析分片列表 -> 下载真实分片测速
    TS / FLV / 裸流: 直接读取码流，校验 MPEG-TS 同步字节并测速

因此能识别出"返回 200 的错误页 / 空播放列表 / 连不上分片 / 龟速源"这几类假可用链接，
同时顺带得到分辨率、码率、实测吞吐，供后续排序使用。
"""

import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
import threading
from typing import Dict, List, Optional, Tuple

from .netclient import HttpClient
from .videoinfo import detect_resolution

_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:(?P<attrs>[^\n]*)", re.IGNORECASE)
_ATTR_RE = re.compile(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)', re.IGNORECASE)
_HTML_HINTS = (b"<html", b"<!doctype", b"<?xml", b"{\"", b"<head")


@dataclass
class ProbeResult:
    url: str = ""
    ok: bool = False
    kind: str = "unknown"          # hls / ts / flv / binary / unknown
    status: int = 0
    ttfb_ms: float = 0.0           # 首包延迟
    kbps: float = 0.0              # 实测码流吞吐
    bytes_read: int = 0
    resolution: str = ""
    bandwidth: int = 0             # manifest 声明码率
    segments: int = 0
    variants: int = 0
    encrypted: bool = False
    # 浏览器能否绕过本站直连：整条链路都是 https 且每一跳都带可用的 CORS 头
    direct: bool = True
    error: str = ""
    elapsed_ms: float = 0.0
    redirects: List[str] = field(default_factory=list)

    @property
    def height(self) -> int:
        if "x" in self.resolution:
            try:
                return int(self.resolution.split("x")[1])
            except (ValueError, IndexError):
                return 0
        return 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "kind": self.kind, "status": self.status,
            "ttfb_ms": round(self.ttfb_ms, 1), "kbps": round(self.kbps, 1),
            "bytes": self.bytes_read, "resolution": self.resolution,
            "bandwidth": self.bandwidth, "segments": self.segments,
            "variants": self.variants, "encrypted": self.encrypted,
            "direct": self.direct,
            "error": self.error, "elapsed_ms": round(self.elapsed_ms, 1),
        }


def classify_exception(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return "http_%d" % exc.code
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            return "timeout"
        if isinstance(reason, socket.gaierror):
            return "dns_error"
        if isinstance(reason, ssl.SSLError):
            return "tls_error"
        text = str(reason).lower()
        if "refused" in text:
            return "conn_refused"
        if "unreachable" in text:
            return "unreachable"
        if "reset" in text:
            return "conn_reset"
        if "timed out" in text:
            return "timeout"
        return "conn_error"
    text = str(exc).lower()
    if "timed out" in text:
        return "timeout"
    if "reset" in text:
        return "conn_reset"
    return exc.__class__.__name__.lower()[:24]


def parse_attrs(text: str) -> dict:
    attrs = {}
    for match in _ATTR_RE.finditer(text):
        attrs[match.group(1).upper()] = match.group(2).strip('"')
    return attrs


def parse_master(manifest: str, base_url: str) -> List[dict]:
    """解析 master playlist，返回按带宽降序的 variant 列表。"""
    variants = []
    lines = manifest.splitlines()
    for index, line in enumerate(lines):
        line = line.strip()
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = parse_attrs(line.split(":", 1)[1] if ":" in line else "")
        target = ""
        for candidate in lines[index + 1:]:
            candidate = candidate.strip()
            if candidate and not candidate.startswith("#"):
                target = candidate
                break
        if not target:
            continue
        try:
            bandwidth = int(attrs.get("BANDWIDTH") or attrs.get("AVERAGE-BANDWIDTH") or 0)
        except ValueError:
            bandwidth = 0
        variants.append({
            "url": urllib.parse.urljoin(base_url, target),
            "bandwidth": bandwidth,
            "resolution": attrs.get("RESOLUTION", ""),
            "codecs": attrs.get("CODECS", ""),
        })
    variants.sort(key=lambda v: v["bandwidth"], reverse=True)
    return variants


def parse_media(manifest: str, base_url: str) -> Tuple[List[str], bool]:
    """解析 media playlist，返回 (分片 URL 列表, 是否加密)。"""
    segments = []
    encrypted = False
    for line in manifest.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            upper = line.upper()
            if upper.startswith("#EXT-X-KEY") and "METHOD=NONE" not in upper:
                encrypted = True
            if upper.startswith("#EXT-X-MAP:"):
                init = parse_attrs(line.split(":", 1)[1]).get("URI", "")
                if init:
                    segments.append(urllib.parse.urljoin(base_url, init))
            continue
        segments.append(urllib.parse.urljoin(base_url, line))
    return segments, encrypted


def looks_like_ts(data: bytes) -> bool:
    if len(data) < 189:
        return bool(data) and data[0] == 0x47
    return data[0] == 0x47 and data[188] == 0x47


def sniff_kind(head: bytes, content_type: str) -> str:
    stripped = head.lstrip()
    if stripped[:7].upper() == b"#EXTM3U":
        return "hls"
    if stripped[:3] == b"FLV":
        return "flv"
    if looks_like_ts(stripped):
        return "ts"
    lowered = stripped[:64].lower()
    for hint in _HTML_HINTS:
        if lowered.startswith(hint):
            return "text"
    ctype = (content_type or "").lower()
    if "mpegurl" in ctype:
        return "hls"
    if ctype.startswith(("video/", "audio/")) or "octet-stream" in ctype or "mp2t" in ctype:
        return "binary"
    if ctype.startswith("text/") or "json" in ctype or "html" in ctype:
        return "text"
    return "binary" if stripped else "empty"


# 这些错误说明"整台主机不可达"，而不是"这条链接不对"
CONNECTION_ERRORS = ("timeout", "conn_refused", "dns_error", "unreachable", "conn_reset",
                     "conn_error", "tls_error")


class Prober:
    def __init__(self, client: HttpClient, cfg: dict):
        self.client = client
        self.cfg = cfg
        self.host_failure_limit = int(cfg.get("host_failure_limit", 8))
        self._host_failures: Dict[str, int] = {}
        self._host_lock = threading.Lock()
        self.connect_timeout = cfg.get("connect_timeout", 6)
        self.probe_timeout = cfg.get("probe_timeout", 14)
        self.probe_bytes = cfg.get("probe_bytes", 256 * 1024)
        self.probe_seconds = cfg.get("probe_seconds", 4.0)
        self.min_bytes = cfg.get("min_bytes", 16 * 1024)
        self.manifest_max_bytes = cfg.get("manifest_max_bytes", 1 << 20)
        self.retries = cfg.get("probe_retries", 0)
        # 探测时带上 Origin，才能看出上游会不会回显/放行我们的域名
        self.origin = (cfg.get("site_url") or "").rstrip("/")

    # ------------------------------------------------------------------ API
    def probe(self, url: str, referer: str = None) -> ProbeResult:
        host = self.client.host_of(url)
        if self._circuit_open(host):
            # 该主机已连续大量连接级失败，直接快速失败，避免整轮被它拖死
            return ProbeResult(url=url, ok=False, error="host_unreachable")

        attempts = self.retries + 1
        result = ProbeResult(url=url, error="unknown")
        for _ in range(attempts):
            sem = self.client.limiter.acquire(host)
            try:
                result = self._probe_once(url, referer)
            finally:
                sem.release()
            self._record_host_result(host, result)
            if result.ok:
                return result
            if result.error in ("http_403", "http_404", "http_410", "dns_error", "not_a_stream"):
                break  # 明确失败，重试无意义
        return result

    def reset_circuits(self) -> None:
        """每轮开始前清空熔断计数。"""
        with self._host_lock:
            self._host_failures.clear()

    def _circuit_open(self, host: str) -> bool:
        if not host or self.host_failure_limit <= 0:
            return False
        with self._host_lock:
            return self._host_failures.get(host, 0) >= self.host_failure_limit

    def _record_host_result(self, host: str, result: ProbeResult) -> None:
        if not host:
            return
        with self._host_lock:
            if result.ok:
                self._host_failures[host] = 0
            elif result.error in CONNECTION_ERRORS:
                self._host_failures[host] = self._host_failures.get(host, 0) + 1
            # HTTP 级错误（404/500）说明主机是活的，不计入熔断

    # -------------------------------------------------------------- 内部实现
    def _probe_once(self, url: str, referer: str = None) -> ProbeResult:
        started = time.time()
        deadline = started + self.probe_timeout
        result = ProbeResult(url=url)
        try:
            self._walk(url, result, deadline, depth=0, referer=referer)
        except Exception as exc:  # noqa: BLE001 - 探测过程中任何异常都只代表"不可用"
            if not result.error:
                result.error = classify_exception(exc)
            result.ok = False
        result.elapsed_ms = (time.time() - started) * 1000
        return result

    def _open(self, url: str, result: ProbeResult, referer: str = None):
        timeout = max(1.0, min(self.connect_timeout, self.probe_timeout))
        headers = {"Origin": self.origin} if self.origin else None
        response, ttfb = self.client.open(url, timeout=timeout, headers=headers,
                                          referer=referer)
        if not result.ttfb_ms:
            result.ttfb_ms = ttfb * 1000
        result.status = response.getcode() or 200
        final_url = response.geturl()
        if final_url != url:
            result.redirects.append(final_url)
        self._note_transport(result, response, final_url)
        return response, final_url

    def _note_transport(self, result: ProbeResult, response, final_url: str) -> None:
        """只要链路上有一跳是 http 或缺 CORS 头，浏览器就没法直连。"""
        if not result.direct:
            return
        if not final_url.lower().startswith("https://"):
            result.direct = False
            return
        allow = (response.headers.get("Access-Control-Allow-Origin") or "").strip()
        if allow == "*":
            return
        if self.origin and allow.rstrip("/") == self.origin:
            return
        result.direct = False

    def _walk(self, url: str, result: ProbeResult, deadline: float, depth: int,
              referer: str = None) -> None:
        if depth > 2:
            result.error = "too_many_redirects"
            return
        if time.time() > deadline:
            result.error = "timeout"
            return

        response = None
        try:
            response, final_url = self._open(url, result, referer)
            head = response.read(8192)
            kind = sniff_kind(head, response.headers.get("Content-Type", ""))

            if kind == "hls":
                body = head + self.client.read_limited(
                    response, self.manifest_max_bytes, deadline)
                manifest = body.decode("utf-8", errors="ignore")
                response.close()
                response = None
                self._handle_hls(manifest, final_url, result, deadline, depth)
                return

            if kind in ("ts", "flv", "binary"):
                result.kind = "ts" if kind == "ts" else kind
                self._measure(response, head, result, deadline)
                return

            if kind == "empty":
                result.error = "empty_response"
                return

            result.error = "not_a_stream"
            result.kind = "text"
        except Exception as exc:  # noqa: BLE001
            result.error = classify_exception(exc)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _handle_hls(self, manifest: str, base_url: str, result: ProbeResult,
                    deadline: float, depth: int) -> None:
        result.kind = "hls"
        variants = parse_master(manifest, base_url)
        if variants:
            result.variants = len(variants)
            best = variants[0]
            result.bandwidth = best["bandwidth"] or result.bandwidth
            result.resolution = best["resolution"] or result.resolution
            self._walk(best["url"], result, deadline, depth + 1, referer=base_url)
            return

        segments, encrypted = parse_media(manifest, base_url)
        result.encrypted = encrypted
        result.segments = len(segments)
        if not segments:
            result.error = "empty_playlist"
            return

        # 直播源取最新分片，失败再退回第一个分片
        candidates = []
        for index in (-1, 0):
            candidate = segments[index]
            if candidate not in candidates:
                candidates.append(candidate)

        last_error = "no_data"
        for candidate in candidates:
            if time.time() > deadline:
                last_error = "timeout"
                break
            response = None
            try:
                response, _ = self._open(candidate, result, referer=base_url)
                head = response.read(8192)
                if not head:
                    last_error = "empty_segment"
                    continue
                self._measure(response, head, result, deadline)
                if result.ok:
                    return
                last_error = result.error or "no_data"
            except Exception as exc:  # noqa: BLE001
                last_error = classify_exception(exc)
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
        result.ok = False
        result.error = last_error

    def _measure(self, response, head: bytes, result: ProbeResult, deadline: float) -> None:
        """读取一段码流并计算吞吐。"""
        started = time.time()
        soft_deadline = min(deadline, started + self.probe_seconds)
        body = self.client.read_limited(response, max(0, self.probe_bytes - len(head)), soft_deadline)
        elapsed = max(time.time() - started, 1e-3)
        total = len(head) + len(body)
        result.bytes_read = total

        if total <= 0:
            result.ok = False
            result.error = "no_data"
            return

        result.kbps = (total * 8.0) / elapsed / 1000.0
        if result.kind in ("binary", "unknown") and looks_like_ts(head):
            result.kind = "ts"

        # manifest 没给 RESOLUTION 时，直接从码流里解出真实分辨率
        if not result.resolution and total >= 4096:
            try:
                result.resolution = detect_resolution(head + body)
            except Exception:  # noqa: BLE001 - 分辨率是可选信息，解析失败不影响判定
                result.resolution = ""

        if total < self.min_bytes:
            # 分片本身可能很小（低码率音频/短分片），只要一次性读完也算有效
            finished = len(body) == 0 and len(head) < 8192
            if not finished:
                result.ok = False
                result.error = "insufficient_data"
                return

        result.ok = True
        result.error = ""
