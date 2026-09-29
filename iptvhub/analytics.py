"""访客与流量统计。

按天聚合，不存原始请求流水——一台小机器没必要为了看趋势去扛一张越滚越大的日志表。
三张表：每日指标、每日访客（去重后每人一行）、每日频道播放数。
后台页面和 /api/admin/analytics 都读这里。
"""

import logging
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .feedback import describe_device, mask_ip

log = logging.getLogger("iptvhub.analytics")

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    day    TEXT NOT NULL,
    metric TEXT NOT NULL,
    value  INTEGER DEFAULT 0,
    PRIMARY KEY (day, metric)
);

CREATE TABLE IF NOT EXISTS daily_visitors (
    day        TEXT NOT NULL,
    client     TEXT NOT NULL,
    ip         TEXT DEFAULT '',
    device     TEXT DEFAULT '',
    requests   INTEGER DEFAULT 0,
    bytes      INTEGER DEFAULT 0,
    plays      INTEGER DEFAULT 0,
    first_seen INTEGER DEFAULT 0,
    last_seen  INTEGER DEFAULT 0,
    PRIMARY KEY (day, client)
);
CREATE INDEX IF NOT EXISTS idx_visitors_day ON daily_visitors(day, bytes DESC);

CREATE TABLE IF NOT EXISTS daily_channels (
    day          TEXT NOT NULL,
    channel_key  TEXT NOT NULL,
    channel_name TEXT DEFAULT '',
    plays        INTEGER DEFAULT 0,
    PRIMARY KEY (day, channel_key)
);
CREATE INDEX IF NOT EXISTS idx_channels_day ON daily_channels(day, plays DESC);
"""

# 指标口径：page=网页打开，playlist=订阅/下载播放列表，api=公开接口调用，
# proxy=中转请求，play=点击播放（前端上报），feedback=提交反馈
METRICS = ("page", "playlist", "api", "proxy", "play", "feedback", "proxy_bytes")


class Analytics:
    def __init__(self, store, secret_provider, flush_every: int = 25,
                 flush_seconds: float = 30.0):
        self.store = store
        self._secret_provider = secret_provider
        self.flush_every = flush_every
        self.flush_seconds = flush_seconds

        with store._connect() as conn:      # noqa: SLF001 - 同包内共用连接
            conn.executescript(SCHEMA)

        self._lock = threading.Lock()
        self._metrics: Dict[str, int] = defaultdict(int)
        self._visitors: Dict[str, Dict[str, Any]] = {}
        self._channels: Dict[str, Dict[str, Any]] = {}
        self._pending = 0
        self._last_flush = time.time()
        self._day = self._today()

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y%m%d")

    def client_id(self, ip: str, user_agent: str = "") -> str:
        import hashlib
        secret = self._secret_provider()
        digest = hashlib.sha256()
        digest.update(secret if isinstance(secret, bytes) else str(secret).encode("utf-8"))
        digest.update(b"|%s|%s" % ((ip or "").encode("utf-8"),
                                   (user_agent or "")[:80].encode("utf-8", "ignore")))
        return digest.hexdigest()[:16]

    # ------------------------------------------------------------------ 记录
    def record(self, kind: str, ip: str = "", user_agent: str = "", size: int = 0,
               channel_key: str = "", channel_name: str = "") -> None:
        now = int(time.time())
        with self._lock:
            if self._today() != self._day:
                self._flush_locked()
                self._day = self._today()

            if kind in METRICS:
                self._metrics[kind] += 1
            if size:
                self._metrics["proxy_bytes"] += size

            if ip:
                client = self.client_id(ip, user_agent)
                entry = self._visitors.get(client)
                if entry is None:
                    entry = {"ip": ip, "device": describe_device(user_agent),
                             "requests": 0, "bytes": 0, "plays": 0,
                             "first_seen": now, "last_seen": now}
                    self._visitors[client] = entry
                entry["requests"] += 1
                entry["bytes"] += max(0, size)
                entry["last_seen"] = now
                if kind == "play":
                    entry["plays"] += 1

            if kind == "play" and channel_key:
                item = self._channels.setdefault(
                    channel_key, {"name": channel_name, "plays": 0})
                item["plays"] += 1
                if channel_name:
                    item["name"] = channel_name

            self._pending += 1
            due = (self._pending >= self.flush_every
                   or time.time() - self._last_flush >= self.flush_seconds)
        if due:
            self.flush()

    # ------------------------------------------------------------------ 落盘
    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        day = self._day
        metrics = list(self._metrics.items())
        visitors = list(self._visitors.items())
        channels = list(self._channels.items())
        self._metrics.clear()
        self._visitors.clear()
        self._channels.clear()
        self._pending = 0
        self._last_flush = time.time()

        try:
            with self.store._write_lock:            # noqa: SLF001
                conn = self.store._connect()        # noqa: SLF001
                with conn:
                    conn.executemany(
                        """INSERT INTO daily_metrics (day, metric, value) VALUES (?,?,?)
                           ON CONFLICT(day, metric) DO UPDATE SET
                               value = value + excluded.value""",
                        [(day, name, count) for name, count in metrics])
                    conn.executemany(
                        """INSERT INTO daily_visitors
                               (day, client, ip, device, requests, bytes, plays,
                                first_seen, last_seen)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(day, client) DO UPDATE SET
                               requests  = requests + excluded.requests,
                               bytes     = bytes + excluded.bytes,
                               plays     = plays + excluded.plays,
                               device    = CASE WHEN excluded.device != '' THEN excluded.device
                                                ELSE device END,
                               last_seen = excluded.last_seen""",
                        [(day, client, item["ip"], item["device"], item["requests"],
                          item["bytes"], item["plays"], item["first_seen"], item["last_seen"])
                         for client, item in visitors])
                    conn.executemany(
                        """INSERT INTO daily_channels (day, channel_key, channel_name, plays)
                           VALUES (?,?,?,?)
                           ON CONFLICT(day, channel_key) DO UPDATE SET
                               plays = plays + excluded.plays,
                               channel_name = CASE WHEN excluded.channel_name != ''
                                                   THEN excluded.channel_name
                                                   ELSE channel_name END""",
                        [(day, key, item["name"], item["plays"]) for key, item in channels])
        except Exception as exc:  # noqa: BLE001 - 统计失败不该影响正常服务
            log.warning("统计落盘失败: %s", exc)

    # ------------------------------------------------------------------ 读取
    def overview(self, days: int = 14) -> Dict[str, Any]:
        self.flush()
        conn = self.store._connect()            # noqa: SLF001
        span = [time.strftime("%Y%m%d", time.localtime(time.time() - offset * 86400))
                for offset in range(days - 1, -1, -1)]

        metrics: Dict[str, Dict[str, int]] = defaultdict(dict)
        placeholders = ",".join("?" * len(span))
        for row in conn.execute(
                "SELECT day, metric, value FROM daily_metrics WHERE day IN (%s)" % placeholders,
                span):
            metrics[row["day"]][row["metric"]] = row["value"]

        uniques: Dict[str, int] = {}
        for row in conn.execute(
                "SELECT day, COUNT(*) AS n FROM daily_visitors WHERE day IN (%s) GROUP BY day"
                % placeholders, span):
            uniques[row["day"]] = row["n"]

        series = []
        for day in span:
            item = metrics.get(day, {})
            series.append({
                "day": "%s-%s" % (day[4:6], day[6:8]),
                "raw_day": day,
                "visitors": uniques.get(day, 0),
                "page": item.get("page", 0),
                "playlist": item.get("playlist", 0),
                "api": item.get("api", 0),
                "play": item.get("play", 0),
                "proxy": item.get("proxy", 0),
                "feedback": item.get("feedback", 0),
                "proxy_gb": round(item.get("proxy_bytes", 0) / float(1 << 30), 3),
                "proxy_mb": round(item.get("proxy_bytes", 0) / float(1 << 20), 1),
            })

        today = series[-1] if series else {}
        totals = {key: sum(row.get(key, 0) for row in series)
                  for key in ("visitors", "page", "playlist", "play", "proxy", "feedback")}
        totals["proxy_gb"] = round(sum(row["proxy_gb"] for row in series), 2)
        return {"series": series, "today": today, "totals": totals, "days": days}

    def visitors(self, day: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        self.flush()
        day = day or self._today()
        rows = self.store._connect().execute(   # noqa: SLF001
            """SELECT * FROM daily_visitors WHERE day = ?
               ORDER BY bytes DESC, requests DESC LIMIT ?""", (day, limit))
        return [{
            "ip": row["ip"],
            "masked": mask_ip(row["ip"]),
            "device": row["device"] or "—",
            "requests": row["requests"],
            "plays": row["plays"],
            "mb": round(row["bytes"] / float(1 << 20), 1),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
        } for row in rows]

    def channels(self, day: Optional[str] = None, limit: int = 15) -> List[Dict[str, Any]]:
        self.flush()
        day = day or self._today()
        rows = self.store._connect().execute(   # noqa: SLF001
            """SELECT channel_key, channel_name, plays FROM daily_channels
               WHERE day = ? ORDER BY plays DESC LIMIT ?""", (day, limit))
        return [dict(row) for row in rows]

    def prune(self, keep_days: int = 180) -> int:
        cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - keep_days * 86400))
        with self.store._write_lock:            # noqa: SLF001
            conn = self.store._connect()        # noqa: SLF001
            with conn:
                removed = 0
                for table in ("daily_metrics", "daily_visitors", "daily_channels"):
                    removed += conn.execute(
                        "DELETE FROM %s WHERE day < ?" % table, (cutoff,)).rowcount
        return removed
