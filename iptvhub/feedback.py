"""播放源反馈：观众可以对每一条源报"能看/卡顿/黑屏/没声音/失效"并留言。

这是探测程序拿不到的信息——机器只能测出"能连上、有码流"，
但画面卡不卡、有没有声音、是不是放的广告，只有真人看得出来。
"""

import hashlib
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

KINDS = {
    "ok": "能正常看",
    "lag": "卡顿/缓冲",
    "black": "黑屏/花屏",
    "nosound": "没有声音",
    "dead": "打不开",
    "other": "其它",
}
GOOD_KINDS = ("ok",)
BAD_KINDS = ("lag", "black", "nosound", "dead")

MAX_MESSAGE = 300
WINDOW_SECONDS = 600
MAX_PER_WINDOW = 10

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 设备识别只用于后台排查（"是不是同一个人在刷"），做粗粒度即可
_DEVICE_HINTS = (
    ("micromessenger", "微信"), ("iphone", "iPhone"), ("ipad", "iPad"),
    ("android", "Android"), ("windows", "Windows"), ("macintosh", "Mac"),
    ("cros", "ChromeOS"), ("linux", "Linux"),
)
_BROWSER_HINTS = (
    ("micromessenger", "微信"), ("edg/", "Edge"), ("firefox", "Firefox"),
    ("chrome", "Chrome"), ("safari", "Safari"), ("vlc", "VLC"), ("curl", "curl"),
)


def mask_ip(ip: str) -> str:
    """公开展示用的打码 IP：1.2.3.* / 2401:abcd:ef::*"""
    ip = (ip or "").strip()
    if not ip:
        return "未知来源"
    if ":" in ip:                                   # IPv6
        parts = [p for p in ip.split(":") if p][:3]
        return ":".join(parts) + "::*" if parts else "未知来源"
    parts = ip.split(".")
    if len(parts) == 4:
        return "%s.%s.%s.*" % (parts[0], parts[1], parts[2])
    return ip


def describe_device(user_agent: str) -> str:
    """从 UA 里粗略提取"设备 · 浏览器"，识别不出就留空。"""
    lowered = (user_agent or "").lower()
    if not lowered:
        return ""
    device = next((label for token, label in _DEVICE_HINTS if token in lowered), "")
    browser = next((label for token, label in _BROWSER_HINTS if token in lowered), "")
    parts = [part for part in (device, browser) if part]
    if len(parts) == 2 and parts[0] == parts[1]:   # 微信内置浏览器会两项都命中
        parts = parts[:1]
    return " · ".join(parts)[:40]


def clean_text(text: Any, limit: int) -> str:
    value = _CONTROL_RE.sub("", str(text or "")).strip()
    value = re.sub(r"\s{3,}", "  ", value)
    return value[:limit]


class FeedbackService:
    def __init__(self, store, secret_provider):
        self.store = store
        self._secret_provider = secret_provider
        self._lock = threading.Lock()
        self._recent: Dict[str, list] = {}

    def client_id(self, remote_ip: str, user_agent: str = "") -> str:
        """对 IP 做带盐哈希，既能限流又不落库明文 IP。"""
        secret = self._secret_provider()
        digest = hashlib.sha256()
        digest.update(secret if isinstance(secret, bytes) else str(secret).encode("utf-8"))
        digest.update(b"|")
        digest.update((remote_ip or "").encode("utf-8"))
        digest.update(b"|")
        digest.update((user_agent or "")[:80].encode("utf-8", errors="ignore"))
        return digest.hexdigest()[:16]

    def _too_fast(self, client: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._recent.get(client, []) if now - t < WINDOW_SECONDS]
            if len(hits) >= MAX_PER_WINDOW:
                self._recent[client] = hits
                return True
            hits.append(now)
            self._recent[client] = hits
        return False

    def submit(self, payload: Dict[str, Any], remote_ip: str,
               user_agent: str = "") -> Tuple[int, Dict[str, Any]]:
        url = str(payload.get("url") or "").strip()
        kind = str(payload.get("kind") or "other").strip().lower()
        message = clean_text(payload.get("message"), MAX_MESSAGE)
        # 身份不接受客户端传值：一律用服务端看到的来源 IP，提交者改不了

        if kind not in KINDS:
            return 400, {"error": "未知的反馈类型"}
        if not url.lower().startswith(("http://", "https://")):
            return 400, {"error": "地址不合法"}
        if kind == "other" and not message:
            return 400, {"error": "请填写具体问题"}

        row = self.store.stream(url)
        if row is None:
            return 404, {"error": "该播放源不在库中"}

        client = self.client_id(remote_ip, user_agent)
        if self._too_fast(client):
            return 429, {"error": "提交太频繁，请稍后再试"}

        since = int(time.time()) - WINDOW_SECONDS
        if self.store.recent_feedback_client(client, since) >= MAX_PER_WINDOW:
            return 429, {"error": "提交太频繁，请稍后再试"}
        if message and self.store.duplicate_feedback(client, url, message, since):
            return 409, {"error": "刚刚已经提交过相同内容"}

        feedback_id = self.store.add_feedback(
            url=url, kind=kind, message=message, nickname=mask_ip(remote_ip),
            channel_key=row["channel_key"], channel_name=row["display_name"], client=client,
            ip=remote_ip, device=describe_device(user_agent))
        return 200, {"id": feedback_id, "message": "感谢反馈",
                     "who": mask_ip(remote_ip)}

    def listing(self, url: str = "", channel_key: str = "",
                limit: int = 30, remote_ip: str = "") -> Dict[str, Any]:
        data = self.store.list_feedback(url=url, channel_key=channel_key,
                                        limit=max(1, min(int(limit), 100)))
        items = []
        for row in data["items"]:
            items.append({
                "id": row["id"],
                "url": row["url"],
                "kind": row["kind"],
                "kind_label": KINDS.get(row["kind"], row["kind"]),
                "message": row["message"],
                # 公开列表只给打码 IP；完整 IP 只在后台可见
                "who": row["nickname"] or mask_ip(row["ip"]),
                "device": row["device"] or "",
                "channel_name": row["channel_name"],
                "created_at": row["created_at"],
            })
        return {
            "total": data["total"],
            "items": items,
            "summary": self.store.feedback_summary(channel_key=channel_key),
            "kinds": KINDS,
            "you": mask_ip(remote_ip) if remote_ip else "",
        }


def score_adjustment(counts: Optional[Dict[str, int]]) -> float:
    """把反馈折算成一个 0.8~1.05 的系数（当前只在后台展示，不自动改排序）。"""
    if not counts:
        return 1.0
    good = sum(counts.get(k, 0) for k in GOOD_KINDS)
    bad = sum(counts.get(k, 0) for k in BAD_KINDS)
    if good + bad == 0:
        return 1.0
    ratio = (good - bad) / float(good + bad)
    return round(max(0.8, min(1.05, 1.0 + 0.05 * ratio * min(1.0, (good + bad) / 5.0))), 4)
