"""SQLite 持久化层。

保存跨轮次的探测历史，这是本服务与"每次全量重测"方案的核心差别：
有了历史才能算稳定性（EWMA 可用率）、连续失败次数、失效冷却，
从而把"偶尔能连一次"的源和"长期稳定"的源区分开。
"""

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS streams (
    url            TEXT PRIMARY KEY,
    channel_key    TEXT NOT NULL,
    display_name   TEXT NOT NULL,
    group_title    TEXT NOT NULL DEFAULT '其他频道',
    logo           TEXT DEFAULT '',
    host           TEXT DEFAULT '',
    scheme         TEXT DEFAULT 'http',
    ip_version     INTEGER DEFAULT 0,
    sources        TEXT DEFAULT '[]',
    source_weight  REAL DEFAULT 1.0,
    first_seen     INTEGER DEFAULT 0,
    last_seen      INTEGER DEFAULT 0,
    checks         INTEGER DEFAULT 0,
    successes      INTEGER DEFAULT 0,
    fail_streak    INTEGER DEFAULT 0,
    last_check_at  INTEGER DEFAULT 0,
    last_ok_at     INTEGER DEFAULT 0,
    ewma           REAL DEFAULT 0.0,
    alive          INTEGER DEFAULT 0,
    kind           TEXT DEFAULT '',
    ttfb_ms        REAL DEFAULT 0,
    kbps           REAL DEFAULT 0,
    resolution     TEXT DEFAULT '',
    bandwidth      INTEGER DEFAULT 0,
    segments       INTEGER DEFAULT 0,
    encrypted      INTEGER DEFAULT 0,
    direct         INTEGER DEFAULT 0,
    last_error     TEXT DEFAULT '',
    score          REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_streams_channel ON streams(channel_key);
CREATE INDEX IF NOT EXISTS idx_streams_alive   ON streams(alive, score DESC);
CREATE INDEX IF NOT EXISTS idx_streams_group   ON streams(group_title);

CREATE TABLE IF NOT EXISTS checks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    url     TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    ok      INTEGER NOT NULL,
    ttfb_ms REAL DEFAULT 0,
    kbps    REAL DEFAULT 0,
    error   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_checks_url_ts ON checks(url, ts);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER DEFAULT 0,
    sources_ok    INTEGER DEFAULT 0,
    sources_total INTEGER DEFAULT 0,
    candidates    INTEGER DEFAULT 0,
    probed        INTEGER DEFAULT 0,
    alive         INTEGER DEFAULT 0,
    channels      INTEGER DEFAULT 0,
    note          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    channel_key  TEXT DEFAULT '',
    channel_name TEXT DEFAULT '',
    kind         TEXT DEFAULT 'other',
    message      TEXT DEFAULT '',
    nickname     TEXT DEFAULT '',
    client       TEXT DEFAULT '',
    created_at   INTEGER NOT NULL,
    hidden       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedback_url     ON feedback(url, id DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_channel ON feedback(channel_key, id DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_time    ON feedback(created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """给老库补上后加的列（SQLite 没有 IF NOT EXISTS 的 ADD COLUMN）。"""
        conn = self._connect()
        existing = {row[1] for row in conn.execute("PRAGMA table_info(streams)")}
        for column, ddl in (("direct", "direct INTEGER DEFAULT 0"),):
            if column not in existing:
                with conn:
                    conn.execute("ALTER TABLE streams ADD COLUMN %s" % ddl)

    # ------------------------------------------------------------- 连接管理
    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ----------------------------------------------------------------- 候选
    def upsert_candidates(self, rows: Iterable[Dict[str, Any]]) -> int:
        """写入本轮采集到的候选源（已存在则只更新元信息与 last_seen）。"""
        now = int(time.time())
        payload = []
        for row in rows:
            payload.append((
                row["url"], row["channel_key"], row["display_name"], row["group_title"],
                row.get("logo", ""), row.get("host", ""), row.get("scheme", "http"),
                int(row.get("ip_version", 0)), json.dumps(sorted(row.get("sources", [])), ensure_ascii=False),
                float(row.get("source_weight", 1.0)), now, now,
            ))
        if not payload:
            return 0
        with self._write_lock:
            conn = self._connect()
            with conn:
                conn.executemany(
                    """
                    INSERT INTO streams (url, channel_key, display_name, group_title, logo, host,
                                         scheme, ip_version, sources, source_weight, first_seen, last_seen)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(url) DO UPDATE SET
                        channel_key=excluded.channel_key,
                        display_name=excluded.display_name,
                        group_title=excluded.group_title,
                        logo=CASE WHEN excluded.logo != '' THEN excluded.logo ELSE streams.logo END,
                        host=excluded.host,
                        scheme=excluded.scheme,
                        ip_version=excluded.ip_version,
                        sources=excluded.sources,
                        source_weight=excluded.source_weight,
                        last_seen=excluded.last_seen
                    """, payload)
        return len(payload)

    # ------------------------------------------------------------- 探测结果
    def record_probes(self, results: Iterable[Dict[str, Any]], alpha: float = 0.4,
                      keep_history: bool = True) -> None:
        now = int(time.time())
        updates, history = [], []
        for item in results:
            url = item["url"]
            probe = item["probe"]
            ok = 1 if probe.ok else 0
            updates.append((
                ok, alpha, now, now if ok else 0,
                probe.kind, probe.ttfb_ms, probe.kbps, probe.resolution,
                int(probe.bandwidth or 0), int(probe.segments or 0),
                1 if probe.encrypted else 0, probe.error or "",
                1 if (probe.ok and probe.direct) else 0, url,
            ))
            if keep_history:
                history.append((url, now, ok, probe.ttfb_ms, probe.kbps, probe.error or ""))

        if not updates:
            return

        with self._write_lock:
            conn = self._connect()
            with conn:
                conn.executemany(
                    """
                    UPDATE streams SET
                        checks        = checks + 1,
                        successes     = successes + ?1,
                        ewma          = ewma * (1 - ?2) + ?1 * ?2,
                        last_check_at = ?3,
                        last_ok_at    = CASE WHEN ?1 = 1 THEN ?4 ELSE last_ok_at END,
                        fail_streak   = CASE WHEN ?1 = 1 THEN 0 ELSE fail_streak + 1 END,
                        alive         = ?1,
                        kind          = CASE WHEN ?1 = 1 THEN ?5 ELSE kind END,
                        ttfb_ms       = CASE WHEN ?1 = 1 THEN ?6 ELSE ttfb_ms END,
                        kbps          = CASE WHEN ?1 = 1 THEN ?7 ELSE kbps END,
                        resolution    = CASE WHEN ?1 = 1 AND ?8 != '' THEN ?8 ELSE resolution END,
                        bandwidth     = CASE WHEN ?1 = 1 AND ?9 > 0 THEN ?9 ELSE bandwidth END,
                        segments      = CASE WHEN ?1 = 1 THEN ?10 ELSE segments END,
                        encrypted     = CASE WHEN ?1 = 1 THEN ?11 ELSE encrypted END,
                        last_error    = ?12,
                        direct        = CASE WHEN ?1 = 1 THEN ?13 ELSE direct END
                    WHERE url = ?14
                    """, updates)
                if history:
                    conn.executemany(
                        "INSERT INTO checks (url, ts, ok, ttfb_ms, kbps, error) VALUES (?,?,?,?,?,?)",
                        history)

    def update_scores(self, scores: Dict[str, float]) -> None:
        if not scores:
            return
        with self._write_lock:
            conn = self._connect()
            with conn:
                conn.executemany("UPDATE streams SET score=? WHERE url=?",
                                 [(value, url) for url, value in scores.items()])

    # ------------------------------------------------------------------ 查询
    def all_streams(self, alive_only: bool = False) -> List[sqlite3.Row]:
        sql = "SELECT * FROM streams"
        if alive_only:
            sql += " WHERE alive=1"
        return list(self._connect().execute(sql))

    def stream(self, url: str) -> Optional[sqlite3.Row]:
        cur = self._connect().execute("SELECT * FROM streams WHERE url=?", (url,))
        return cur.fetchone()

    def cooldown_urls(self, hours: int) -> set:
        """近期刚测过且失败的链接，本轮跳过，把时间留给更有希望的源。"""
        cutoff = int(time.time()) - hours * 3600
        cur = self._connect().execute(
            "SELECT url FROM streams WHERE alive=0 AND fail_streak >= 3 AND last_check_at > ?",
            (cutoff,))
        return {row[0] for row in cur}

    def history(self, url: str, limit: int = 48) -> List[sqlite3.Row]:
        return list(self._connect().execute(
            "SELECT ts, ok, ttfb_ms, kbps, error FROM checks WHERE url=? ORDER BY ts DESC LIMIT ?",
            (url, limit)))

    def stats(self) -> Dict[str, Any]:
        conn = self._connect()
        row = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(alive) alive,
                      COUNT(DISTINCT channel_key) channels,
                      AVG(CASE WHEN alive=1 THEN ttfb_ms END) avg_ttfb,
                      AVG(CASE WHEN alive=1 THEN kbps END) avg_kbps
               FROM streams""").fetchone()
        groups = conn.execute(
            """SELECT group_title, COUNT(DISTINCT channel_key) channels, SUM(alive) streams
               FROM streams WHERE alive=1 GROUP BY group_title""").fetchall()
        last_run = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "streams_total": row["total"] or 0,
            "streams_alive": row["alive"] or 0,
            "channels_total": row["channels"] or 0,
            "avg_ttfb_ms": round(row["avg_ttfb"] or 0, 1),
            "avg_kbps": round(row["avg_kbps"] or 0, 1),
            "groups": [dict(g) for g in groups],
            "last_run": dict(last_run) if last_run else None,
        }

    def recent_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        return [dict(row) for row in self._connect().execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ 反馈
    def add_feedback(self, url: str, kind: str, message: str = "", nickname: str = "",
                     channel_key: str = "", channel_name: str = "", client: str = "") -> int:
        with self._write_lock:
            conn = self._connect()
            with conn:
                cur = conn.execute(
                    """INSERT INTO feedback (url, channel_key, channel_name, kind, message,
                                             nickname, client, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (url, channel_key, channel_name, kind, message, nickname, client,
                     int(time.time())))
                return cur.lastrowid

    def list_feedback(self, url: str = "", channel_key: str = "", kind: str = "",
                      include_hidden: bool = False, limit: int = 50,
                      offset: int = 0) -> Dict[str, Any]:
        where, params = [], []
        if url:
            where.append("url = ?")
            params.append(url)
        if channel_key:
            where.append("channel_key = ?")
            params.append(channel_key)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if not include_hidden:
            where.append("hidden = 0")
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM feedback %s" % clause, params).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM feedback %s ORDER BY id DESC LIMIT ? OFFSET ?" % clause,
            params + [int(limit), int(offset)]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def feedback_summary(self, channel_key: str = "") -> Dict[str, Dict[str, int]]:
        """按 URL 汇总各类反馈数量，用于在播放列表里显示角标。"""
        sql = ("SELECT url, kind, COUNT(*) AS count FROM feedback "
               "WHERE hidden = 0 %s GROUP BY url, kind")
        params: List[Any] = []
        if channel_key:
            sql = sql % "AND channel_key = ?"
            params.append(channel_key)
        else:
            sql = sql % ""
        summary: Dict[str, Dict[str, int]] = {}
        for row in self._connect().execute(sql, params):
            summary.setdefault(row["url"], {})[row["kind"]] = row["count"]
        return summary

    def recent_feedback_client(self, client: str, since: int) -> int:
        row = self._connect().execute(
            "SELECT COUNT(*) FROM feedback WHERE client = ? AND created_at > ?",
            (client, since)).fetchone()
        return row[0] if row else 0

    def duplicate_feedback(self, client: str, url: str, message: str, since: int) -> bool:
        row = self._connect().execute(
            """SELECT 1 FROM feedback
               WHERE client = ? AND url = ? AND message = ? AND created_at > ? LIMIT 1""",
            (client, url, message, since)).fetchone()
        return bool(row)

    def set_feedback_hidden(self, feedback_id: int, hidden: bool) -> int:
        with self._write_lock:
            conn = self._connect()
            with conn:
                return conn.execute("UPDATE feedback SET hidden=? WHERE id=?",
                                    (1 if hidden else 0, int(feedback_id))).rowcount

    def delete_feedback(self, feedback_id: int) -> int:
        with self._write_lock:
            conn = self._connect()
            with conn:
                return conn.execute("DELETE FROM feedback WHERE id=?",
                                    (int(feedback_id),)).rowcount

    def feedback_stats(self) -> Dict[str, int]:
        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        hidden = conn.execute("SELECT COUNT(*) FROM feedback WHERE hidden=1").fetchone()[0]
        day = conn.execute("SELECT COUNT(*) FROM feedback WHERE created_at > ?",
                           (int(time.time()) - 86400,)).fetchone()[0]
        return {"total": total, "hidden": hidden, "last_24h": day}

    # -------------------------------------------------------------- 后台查询
    def query_streams(self, q: str = "", group: str = "", alive: Optional[int] = None,
                      error: str = "", channel_key: str = "", order: str = "score",
                      limit: int = 200, offset: int = 0) -> Dict[str, Any]:
        """管理后台的源级检索。"""
        where, params = [], []
        if q:
            where.append("(display_name LIKE ? OR url LIKE ? OR host LIKE ?)")
            like = "%%%s%%" % q
            params += [like, like, like]
        if group:
            where.append("group_title = ?")
            params.append(group)
        if alive is not None:
            where.append("alive = ?")
            params.append(int(alive))
        if error:
            where.append("last_error = ?")
            params.append(error)
        if channel_key:
            where.append("channel_key = ?")
            params.append(channel_key)

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        orders = {
            "score": "score DESC", "latency": "ttfb_ms ASC", "kbps": "kbps DESC",
            "checks": "checks DESC", "name": "display_name ASC", "recent": "last_check_at DESC",
        }
        order_by = orders.get(order, orders["score"])

        conn = self._connect()
        total = conn.execute("SELECT COUNT(*) FROM streams %s" % clause, params).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM streams %s ORDER BY %s LIMIT ? OFFSET ?" % (clause, order_by),
            params + [int(limit), int(offset)]).fetchall()
        return {"total": total, "rows": [dict(r) for r in rows]}

    def source_stats(self) -> Dict[str, Dict[str, int]]:
        """每个上游源贡献了多少候选 / 多少可用（sources 列是 JSON 数组）。"""
        stats: Dict[str, Dict[str, int]] = {}
        for row in self._connect().execute("SELECT sources, alive FROM streams"):
            try:
                names = json.loads(row["sources"] or "[]")
            except (ValueError, TypeError):
                names = []
            for name in names:
                entry = stats.setdefault(name, {"candidates": 0, "alive": 0})
                entry["candidates"] += 1
                entry["alive"] += int(row["alive"] or 0)
        return stats

    def error_breakdown(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._connect().execute(
            """SELECT last_error AS error, COUNT(*) AS count FROM streams
               WHERE alive = 0 AND last_check_at > 0 AND last_error != ''
               GROUP BY last_error ORDER BY count DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]

    def group_names(self) -> List[str]:
        return [r[0] for r in self._connect().execute(
            "SELECT DISTINCT group_title FROM streams ORDER BY group_title")]

    def delete_stream(self, url: str) -> int:
        with self._write_lock:
            conn = self._connect()
            with conn:
                removed = conn.execute("DELETE FROM streams WHERE url = ?", (url,)).rowcount
                conn.execute("DELETE FROM checks WHERE url = ?", (url,))
        return removed

    # ------------------------------------------------------------------ 运行
    def start_run(self) -> int:
        with self._write_lock:
            conn = self._connect()
            with conn:
                cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (int(time.time()),))
                return cur.lastrowid

    def finish_run(self, run_id: int, **fields) -> None:
        allowed = ("sources_ok", "sources_total", "candidates", "probed", "alive", "channels", "note")
        sets = ["finished_at=?"]
        values: List[Any] = [int(time.time())]
        for key in allowed:
            if key in fields:
                sets.append("%s=?" % key)
                values.append(fields[key])
        values.append(run_id)
        with self._write_lock:
            conn = self._connect()
            with conn:
                conn.execute("UPDATE runs SET %s WHERE id=?" % ", ".join(sets), values)

    # ------------------------------------------------------------------ 维护
    def prune(self, prune_after_days: int, prune_fail_streak: int,
              keep_history_days: int = 14) -> Dict[str, int]:
        now = int(time.time())
        dead_cutoff = now - prune_after_days * 86400
        history_cutoff = now - keep_history_days * 86400
        with self._write_lock:
            conn = self._connect()
            with conn:
                dead = conn.execute(
                    """DELETE FROM streams
                       WHERE alive=0 AND fail_streak >= ?
                         AND (last_ok_at = 0 OR last_ok_at < ?)
                         AND last_seen < ?""",
                    (prune_fail_streak, dead_cutoff, dead_cutoff)).rowcount
                stale = conn.execute("DELETE FROM checks WHERE ts < ?", (history_cutoff,)).rowcount
                conn.execute(
                    "DELETE FROM checks WHERE url NOT IN (SELECT url FROM streams)")
        return {"streams_removed": dead, "checks_removed": stale}

    def set_meta(self, key: str, value: str) -> None:
        with self._write_lock:
            conn = self._connect()
            with conn:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str, default: str = "") -> str:
        cur = self._connect().execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default
