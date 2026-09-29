"""HTTP 服务：网页 + JSON API + 动态播放列表。

仅用标准库实现，可直接裸跑，也可以放在 nginx 后面。
相比"把 m3u 文件丢到静态托管"的做法，这里能按分组/画质/IP 版本/关键词
动态生成播放列表，播放器直接订阅一个带参数的 URL 即可。
"""

import json
import logging
import os
import signal
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from . import export
from .admin import AdminApi
from .analytics import Analytics
from .auth import COOKIE_NAME, Auth
from .config import load_config
from .feedback import FeedbackService
from .netclient import HttpClient
from .notices import load_active
from .proxy import StreamProxy
from .runtime import RUN_STATE, attach_log_ring
from .store import Store
from .util import human_time

log = logging.getLogger("iptvhub.server")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class ChannelCache:
    """按 mtime 缓存 channels.json，避免每个请求都重新解析。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._payload: Dict[str, Any] = {"channels": [], "generated_at": "", "stats": {}}

    def get(self) -> Dict[str, Any]:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return self._payload
        if mtime != self._mtime:
            with self._lock:
                if mtime != self._mtime:
                    try:
                        with open(self.path, "r", encoding="utf-8") as handle:
                            self._payload = json.load(handle)
                        self._mtime = mtime
                    except (ValueError, OSError) as exc:
                        log.warning("读取 channels.json 失败: %s", exc)
        return self._payload

    @property
    def channels(self) -> List[Dict[str, Any]]:
        return self.get().get("channels", [])


class Updater:
    """后台更新线程：定时执行流水线，并支持手动触发。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.interval = max(1, int(cfg["server"].get("update_interval_hours", 4))) * 3600
        self.enabled = bool(cfg["server"].get("auto_update", True))
        self._lock = threading.Lock()
        self._running = False
        self._last: Dict[str, Any] = {}
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_result(self) -> Dict[str, Any]:
        return dict(self._last)

    def trigger(self, **kwargs) -> bool:
        """手动触发一次更新，已有任务在跑时返回 False。"""
        if not self._lock.acquire(blocking=False):
            return False
        thread = threading.Thread(target=self._run_once, kwargs=kwargs,
                                  name="iptvhub-update", daemon=True)
        thread.start()
        return True

    def _run_once(self, **kwargs) -> None:
        from .pipeline import Pipeline
        self._running = True
        started = time.time()
        try:
            # 重新读盘：管理后台改过的参数下一轮就生效，不必重启服务
            summary = Pipeline(load_config()).run(**kwargs)
            summary["finished_at"] = human_time()
            self._last = summary
        except Exception as exc:  # noqa: BLE001
            log.exception("后台更新失败")
            self._last = {"error": str(exc), "finished_at": human_time()}
        finally:
            self._running = False
            log.info("后台更新结束，耗时 %.1fs", time.time() - started)
            self._lock.release()

    def loop(self) -> None:
        if not self.enabled:
            return
        store = Store(self.cfg["paths"]["db"])
        while not self._stop.is_set():
            try:
                last_run = int(store.get_meta("last_run_at", "0") or 0)
            except ValueError:
                last_run = 0
            due = time.time() - last_run >= self.interval
            if due and not self._running:
                log.info("定时更新触发（距上次 %s）",
                         human_time(last_run) if last_run else "从未运行")
                self.trigger(trigger="schedule")
            self._stop.wait(60)

    def shutdown(self) -> None:
        self._stop.set()


def make_handler(cfg: dict, cache: ChannelCache, store: Store, updater: Updater,
                 admin: AdminApi, proxy: StreamProxy, feedback: FeedbackService,
                 analytics: Analytics, auth: Auth):
    web_dir = cfg["paths"]["web"]
    data_dir = cfg["paths"]["data"]
    admin_token = (cfg["server"].get("admin_token") or "").strip()
    epg_url = cfg.get("epg_url", "")
    site_url = cfg.get("site_url", "")

    class Handler(BaseHTTPRequestHandler):
        server_version = "IPTVHub"
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------- 响应工具
        def _send(self, body: bytes, status: int = 200, ctype: str = "text/plain; charset=utf-8",
                  extra: Optional[dict] = None, cache_control: str = "no-cache") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", cache_control)
            if getattr(self, "_extra_cookie", ""):
                self.send_header("Set-Cookie", self._extra_cookie)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(body, status, "application/json; charset=utf-8")

        def _text(self, text: str, status: int = 200,
                  ctype: str = "text/plain; charset=utf-8", filename: str = "",
                  cache_control: str = "no-cache") -> None:
            extra = {}
            if filename:
                extra["Content-Disposition"] = 'inline; filename="%s"' % filename
            self._send(text.encode("utf-8"), status, ctype, extra, cache_control)

        def _file(self, path: str) -> None:
            if not os.path.isfile(path):
                self._json({"error": "not found"}, 404)
                return
            ext = os.path.splitext(path)[1].lower()
            with open(path, "rb") as handle:
                body = handle.read()
            self._send(body, 200, CONTENT_TYPES.get(ext, "application/octet-stream"))

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

        # --------------------------------------------------------- 路由
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(parsed.query)

            # /admin/ 这类带尾斜杠的地址会让页面里的相对资源解析成 /admin/static/...
            # 直接 301 回无斜杠形式，避免"页面打开了但脚本 404"
            if parsed.path != path and parsed.path != "/":
                target = path + (("?" + parsed.query) if parsed.query else "")
                self.send_response(301)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            try:
                if not self._gate(path):
                    return
                self._track(path)
                self._route(path, query)
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.exception("请求处理失败 %s", self.path)
                self._json({"error": str(exc)}, 500)

        do_HEAD = do_GET

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Range, Content-Type, X-Admin-Token")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ------------------------------------------------------------ 中转
        def _proxy(self, query: Dict[str, List[str]]) -> None:
            if not proxy.enabled:
                self._json({"error": "proxy disabled"}, 404)
                return

            url = query.get("u", [""])[0]
            signature = query.get("s", [""])[0]
            if not url.lower().startswith(("http://", "https://")):
                self._json({"error": "bad url"}, 400)
                return
            if not proxy.authorized(url, signature):
                self._json({"error": "forbidden"}, 403)
                return

            client = self._client_ip()
            allowed, reason = proxy.meter.check(client)
            if not allowed:
                self._json({"error": reason, "quota": True}, 429)
                return
            if not proxy.acquire():
                proxy.meter.done(client)
                self._json({"error": "当前预览人数已满，请稍后再试"}, 503)
                return

            response = None
            try:
                headers = {}
                client_range = self.headers.get("Range")
                if client_range:
                    headers["Range"] = client_range
                response, final_url, head = proxy.open_upstream(url, headers=headers)
                ctype = response.headers.get("Content-Type", "")

                if proxy.looks_like_manifest(head, ctype):
                    deadline = time.time() + float(cfg.get("fetch_timeout", 15))
                    body = head + proxy.client.read_limited(
                        response, proxy.manifest_max_bytes, deadline)
                    text = body.decode("utf-8", errors="ignore")
                    rewritten = proxy.rewrite_manifest(text, final_url)
                    payload = rewritten.encode("utf-8")
                    proxy.meter.add(client, len(payload))
                    self._send(payload, 200,
                               "application/vnd.apple.mpegurl; charset=utf-8",
                               cache_control="no-cache")
                    return

                status = response.getcode() or 200
                self.send_response(status)
                self.send_header("Content-Type", ctype or "video/mp2t")
                length = response.headers.get("Content-Length")
                chunked = not length
                if length:
                    self.send_header("Content-Length", length)
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                for name in ("Content-Range", "Accept-Ranges"):
                    value = response.headers.get(name)
                    if value:
                        self.send_header(name, value)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=10")
                self.end_headers()
                self._pump(response, head, chunked, client)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("中转失败 %s: %s", url, exc)
                try:
                    self._json({"error": "upstream error", "detail": str(exc)}, 502)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001
                        pass
                proxy.release()
                proxy.meter.done(client)

        def _pump(self, response, head: bytes, chunked: bool, client: str) -> None:
            """把上游码流边读边写给浏览器；客户端断开、超量或超时即结束。

            HLS 是一片一个请求，天然短；裸 TS 流则可能一直不断，
            所以这里对单次请求的字节数与时长都设上限。
            """
            def write(piece: bytes) -> None:
                if chunked:
                    self.wfile.write(b"%X\r\n" % len(piece))
                    self.wfile.write(piece)
                    self.wfile.write(b"\r\n")
                else:
                    self.wfile.write(piece)

            meter = proxy.meter
            deadline = time.time() + meter.max_request_seconds
            sent = 0
            try:
                if head:
                    write(head)
                    sent += len(head)
                while True:
                    if sent >= meter.max_request_bytes or time.time() > deadline:
                        log.debug("中转单请求达到上限，主动结束 (%d 字节)", sent)
                        break
                    piece = response.read(proxy.chunk_size)
                    if not piece:
                        break
                    write(piece)
                    sent += len(piece)
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                meter.add(client, sent)
                analytics.record("proxy", client, self.headers.get("User-Agent", ""),
                                 size=sent)

        def _track(self, path: str) -> None:
            """按路径把请求归类记账；后台页面与其轮询接口不算访客。"""
            if path.startswith("/admin") or path.startswith("/api/admin"):
                return
            if path.startswith("/static") or path in ("/healthz", "/favicon.ico"):
                return
            if path == "/proxy":
                return                      # 中转在 _proxy 里连同字节数一起记
            if path == "/":
                kind = "page"
            elif path.startswith("/playlist") or path == "/channel.m3u":
                kind = "playlist"
            elif path.startswith("/api/"):
                kind = "api"
            else:
                return
            analytics.record(kind, self._client_ip(), self.headers.get("User-Agent", ""))

        # ----------------------------------------------------------- 鉴权
        def _cookie(self, name: str) -> str:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")
                if key == name:
                    return urllib.parse.unquote(value)
            return ""

        def _user(self) -> Optional[Dict[str, Any]]:
            """当前请求的登录用户；同一请求内只查一次。"""
            if getattr(self, "_cached_user", "miss") != "miss":
                return self._cached_user
            user = auth.session_user(self._cookie(COOKIE_NAME))
            if user is None:
                # 播放器没法登录，允许用账号的订阅密钥访问播放列表
                key = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query).get("key", [""])[0]
                if key:
                    user = auth.user_by_key(key)
            self._cached_user = user
            return user

        def _is_admin(self) -> bool:
            user = self._user()
            return bool(user and user.get("role") == "admin")

        def _set_session_cookie(self, token: str, days: int) -> None:
            parts = [
                "%s=%s" % (COOKIE_NAME, token),
                "Path=/",
                "HttpOnly",
                "SameSite=Lax",
                "Max-Age=%d" % (days * 86400 if token else 0),
            ]
            if (self.headers.get("X-Forwarded-Proto", "") or "").lower() == "https":
                parts.append("Secure")
            self._extra_cookie = "; ".join(parts)

        def _gate(self, path: str) -> bool:
            """返回 True 表示已放行；False 表示本方法已经回过响应了。"""
            if path in ("/login", "/healthz", "/api/login", "/api/logout", "/api/me"):
                return True
            if path.startswith("/static/"):
                return True

            if path.startswith("/admin") or path.startswith("/api/admin"):
                if self._is_admin():
                    return True
                token = (self.headers.get("X-Admin-Token", "")
                         or urllib.parse.parse_qs(
                             urllib.parse.urlsplit(self.path).query).get("token", [""])[0])
                if admin.authorized(token):      # 留给脚本/自动化用的令牌
                    return True
                if path.startswith("/admin"):
                    self._redirect("/login?next=/admin")
                else:
                    self._json({"error": "需要管理员身份", "login": True}, 401)
                return False

            if not auth.require_login or self._user():
                return True

            if path == "/":
                self._redirect("/login")
            else:
                self._json({"error": "请先登录", "login": True}, 401)
            return False

        def _redirect(self, target: str, status: int = 302) -> None:
            self.send_response(status)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            if getattr(self, "_extra_cookie", ""):
                self.send_header("Set-Cookie", self._extra_cookie)
            self.end_headers()

        def _client_ip(self) -> str:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
            return self.client_address[0] if self.client_address else ""

        def _read_body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 4 << 20:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if path == "/api/login":
                    body = self._read_body()
                    try:
                        user = auth.authenticate(str(body.get("username") or ""),
                                                 str(body.get("password") or ""),
                                                 self._client_ip())
                    except PermissionError as exc:
                        self._json({"error": str(exc)}, 429)
                        return
                    if not user:
                        self._json({"error": "用户名或密码不正确"}, 401)
                        return
                    token = auth.create_session(user["username"], self._client_ip(),
                                                self.headers.get("User-Agent", ""))
                    self._set_session_cookie(token, auth.session_days)
                    analytics.record("login", self._client_ip(),
                                     self.headers.get("User-Agent", ""))
                    self._json({"user": auth.public(user)})
                    return

                if path == "/api/logout":
                    auth.destroy_session(self._cookie(COOKIE_NAME))
                    self._set_session_cookie("", 0)
                    self._json({"ok": True})
                    return

                if not self._gate(path):
                    return

                if path.startswith("/api/admin"):
                    self._admin(path, query, "POST")
                    return
                if path == "/api/track":
                    body = self._read_body()
                    event = str(body.get("event") or "")[:16]
                    if event == "play":
                        analytics.record("play", self._client_ip(),
                                         self.headers.get("User-Agent", ""),
                                         channel_key=str(body.get("key") or "")[:64],
                                         channel_name=str(body.get("name") or "")[:64])
                    self._json({"ok": True})
                    return

                if path == "/api/feedback":
                    status, result = feedback.submit(
                        self._read_body(), self._client_ip(),
                        self.headers.get("User-Agent", ""))
                    if status == 200:
                        analytics.record("feedback", self._client_ip(),
                                         self.headers.get("User-Agent", ""))
                    self._json(result, status)
                    return
                if path == "/api/update":
                    # 旧接口保留：nginx 只放行本机；另可用 admin_token
                    token = (query.get("token", [""])[0]
                             or self.headers.get("X-Admin-Token", ""))
                    if admin_token and token != admin_token:
                        self._json({"error": "unauthorized"}, 401)
                        return
                    started = updater.trigger(trigger="api")
                    self._json({"started": started, "running": updater.running},
                               202 if started else 409)
                    return
                self._json({"error": "not found"}, 404)
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.exception("请求处理失败 %s", self.path)
                self._json({"error": str(exc)}, 500)

        def _admin(self, path: str, query: Dict[str, List[str]], method: str) -> None:
            # 能走到这里说明 _gate 已经确认是管理员（或持有管理令牌）
            body = self._read_body() if method == "POST" else {}
            sub = path[len("/api/admin"):] or "/"
            sub = sub.rstrip("/") or "/"
            status, payload = admin.handle(method, sub, query, body)
            self._json(payload, status)

        # ---------------------------------------------------------- 分发
        def _route(self, path: str, query: Dict[str, List[str]]) -> None:
            if path == "/":
                self._file(os.path.join(web_dir, "index.html"))
                return
            if path == "/login":
                self._file(os.path.join(web_dir, "login.html"))
                return
            if path == "/admin":
                self._file(os.path.join(web_dir, "admin.html"))
                return
            if path.startswith("/api/admin"):
                self._admin(path, query, "GET")
                return
            if path == "/healthz":
                self._json({"ok": True, "time": human_time(), "updating": updater.running})
                return
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                safe = os.path.normpath(os.path.join(web_dir, rel))
                if not safe.startswith(os.path.abspath(web_dir)):
                    self._json({"error": "forbidden"}, 403)
                    return
                self._file(safe)
                return

            if path == "/proxy":
                self._proxy(query)
                return

            if path == "/channel.m3u":
                key = query.get("key", [""])[0]
                name = query.get("name", [""])[0]
                channels = [c for c in cache.channels
                            if (key and c.get("key") == key)
                            or (name and c.get("name") == name)]
                if not channels:
                    self._json({"error": "未找到该频道"}, 404)
                    return
                content = export.render_m3u(channels, True, epg_url=epg_url,
                                            site_url=site_url)
                self._text(content, ctype="audio/x-mpegurl; charset=utf-8",
                           filename="%s.m3u" % (channels[0].get("name") or "channel"))
                return

            if path.startswith("/playlist"):
                self._playlist(path, query)
                return

            if path.startswith("/api/"):
                self._api(path, query)
                return

            # 直接暴露已生成的文件（playlist_full.m3u 等）
            candidate = os.path.normpath(os.path.join(data_dir, path.lstrip("/")))
            if candidate.startswith(os.path.abspath(data_dir)) and os.path.isfile(candidate):
                self._file(candidate)
                return

            self._json({"error": "not found", "path": path}, 404)

        # ------------------------------------------------------ 播放列表
        def _playlist(self, path: str, query: Dict[str, List[str]]) -> None:
            channels = self._filtered(query)
            backups = query.get("backups", ["0"])[0] not in ("0", "false", "")
            generated = cache.get().get("generated_at", human_time())
            fmt = path.rsplit(".", 1)[-1] if "." in path else "m3u"

            # 订阅器通常几分钟就拉一次，给个短缓存，回源压力小很多
            cache_header = "public, max-age=120"
            if fmt == "txt":
                self._text(export.render_txt(channels, backups), filename="playlist.txt",
                           cache_control=cache_header)
                return
            if fmt == "json":
                self._json({"generated_at": generated, "count": len(channels),
                            "channels": channels})
                return
            content = export.render_m3u(channels, backups, epg_url=epg_url,
                                        generated_at=generated, site_url=site_url)
            self._text(content, ctype="audio/x-mpegurl; charset=utf-8",
                       filename="playlist.%s" % ("m3u8" if fmt == "m3u8" else "m3u"),
                       cache_control=cache_header)

        def _filtered(self, query: Dict[str, List[str]],
                      apply_limit: bool = True) -> List[Dict[str, Any]]:
            def one(key: str, default: str = "") -> str:
                return query.get(key, [default])[0]

            def number(key: str, default: float = 0.0) -> float:
                try:
                    return float(one(key) or default)
                except ValueError:
                    return default

            return export.filter_channels(
                cache.channels,
                group=one("group"),
                query=one("q"),
                ip_version=int(number("ipv")),
                min_score=number("min_score"),
                min_height=int(number("min_height")),
                limit=int(number("limit")) if apply_limit else 0,
            )

        # ------------------------------------------------------------ API
        def _api(self, path: str, query: Dict[str, List[str]]) -> None:
            payload = cache.get()

            if path == "/api/stats":
                stats = store.stats()
                stats["run"] = RUN_STATE.snapshot()
                stats["generated_at"] = payload.get("generated_at", "")
                stats["channel_count"] = len(payload.get("channels", []))
                stats["updating"] = updater.running
                stats["site_url"] = site_url
                stats["last_update_result"] = updater.last_result
                self._json(stats)
                return

            if path == "/api/groups":
                counter: Dict[str, int] = {}
                for channel in payload.get("channels", []):
                    counter[channel.get("group", "其他频道")] = counter.get(
                        channel.get("group", "其他频道"), 0) + 1
                order = payload.get("groups", [])
                groups = [{"name": name, "count": count} for name, count in counter.items()]
                groups.sort(key=lambda g: (order.index(g["name"]) if g["name"] in order else 999))
                self._json({"groups": groups, "total": len(payload.get("channels", []))})
                return

            if path == "/api/channels":
                # 分页在过滤之后做，limit 不能提前截断，否则 offset 会落空
                channels = self._filtered(query, apply_limit=False)
                offset = int(float(query.get("offset", ["0"])[0] or 0))
                limit = int(float(query.get("limit", ["0"])[0] or 0))
                total = len(channels)
                if offset:
                    channels = channels[offset:]
                if limit:
                    channels = channels[:limit]
                self._json({"total": total, "count": len(channels),
                            "generated_at": payload.get("generated_at", ""),
                            "channels": channels})
                return

            if path == "/api/me":
                self._json({"user": auth.public(self._user()),
                            "require_login": auth.require_login})
                return

            if path == "/api/notice":
                self._json({"notice": load_active(cfg["paths"]["config"])})
                return

            if path == "/api/feedback":
                self._json(feedback.listing(
                    url=query.get("url", [""])[0],
                    channel_key=query.get("channel", [""])[0],
                    limit=int(float(query.get("limit", ["30"])[0] or 30)),
                    remote_ip=self._client_ip()))
                return

            if path == "/api/runs":
                self._json({"runs": store.recent_runs(20)})
                return

            if path == "/api/history":
                url = query.get("url", [""])[0]
                if not url:
                    self._json({"error": "url required"}, 400)
                    return
                rows = [dict(row) for row in store.history(url, 96)]
                self._json({"url": url, "checks": rows})
                return

            if path == "/api/sources":
                from .config import load_sources
                self._json({"sources": load_sources()})
                return

            self._json({"error": "not found", "path": path}, 404)

    return Handler


def serve(cfg: dict) -> None:
    host = cfg["server"].get("host", "0.0.0.0")
    port = int(cfg["server"].get("port", 8088))
    cache = ChannelCache(os.path.join(cfg["paths"]["data"], "channels.json"))
    store = Store(cfg["paths"]["db"])
    updater = Updater(cfg)
    proxy = StreamProxy(cfg, store, HttpClient(cfg))
    admin = AdminApi(cfg, store, updater, cache, proxy)
    feedback = FeedbackService(store, proxy.secret)
    analytics = Analytics(store, proxy.secret)
    auth = Auth(store, cfg)
    admin.feedback = feedback
    admin.analytics = analytics
    admin.auth = auth
    attach_log_ring()

    handler = make_handler(cfg, cache, store, updater, admin, proxy, feedback,
                           analytics, auth)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    # systemd stop/restart 发的是 SIGTERM，默认会直接杀掉进程，
    # 中转流量计数就丢了；这里接管信号，先落盘再退出。
    def _shutdown(signum, _frame):
        log.info("收到信号 %s，正在退出…", signum)
        proxy.meter.flush()
        analytics.flush()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):  # pragma: no cover - 非主线程时忽略
            pass

    if updater.enabled:
        threading.Thread(target=updater.loop, name="iptvhub-scheduler", daemon=True).start()
        log.info("内置定时更新已启用，每 %d 小时一次", updater.interval // 3600)

    base = "http://%s:%d" % (host if host != "0.0.0.0" else "127.0.0.1", port)
    log.info("IPTV-Hub 服务已启动: %s", base)
    token = admin.token()
    log.info("账号数: %d（用 python3 -m iptvhub user list 查看）", auth.count())
    log.info("管理后台: %s/admin  令牌已就绪（%s，或执行 python3 -m iptvhub token 查看）",
             base, "来自 config.json" if cfg["server"].get("admin_token") else admin.token_path)
    del token
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断信号，正在退出…")
    finally:
        updater.shutdown()
        proxy.meter.flush()
        analytics.flush()
        httpd.server_close()
