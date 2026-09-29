"""账号、登录与会话。

两种角色：
  admin —— 可进 /admin，管理源、参数、反馈、统计与账号
  user  —— 只能看前台（浏览频道、播放、订阅、反馈）

密码用 PBKDF2-HMAC-SHA256 加盐存储，**任何地方都不保存明文**；
会话是服务端签发的随机令牌，存库可吊销，Cookie 带 HttpOnly + SameSite。
每个账号另有一个订阅密钥，供 VLC 这类播放器在 URL 里带着用（播放器没法登录）。
"""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("iptvhub.auth")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    password   TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'user',
    sub_key    TEXT NOT NULL DEFAULT '',
    note       TEXT DEFAULT '',
    disabled   INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT 0,
    last_login INTEGER DEFAULT 0,
    last_ip    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_users_key ON users(sub_key);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ip         TEXT DEFAULT '',
    device     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at);
"""

ROLES = ("admin", "user")
ITERATIONS = 200_000
COOKIE_NAME = "iptv_session"

# 登录限流：同一 IP 连续失败太多次就先冷一会儿
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = 600


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (ITERATIONS, salt.hex(), digest.hex())


def verify_password(stored: str, password: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = (stored or "").split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                       bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(computed.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


class Auth:
    def __init__(self, store, cfg: Optional[dict] = None):
        self.store = store
        cfg = cfg or {}
        self.session_days = int(cfg.get("session_days", 14))
        self.require_login = bool(cfg.get("require_login", True))
        with store._connect() as conn:          # noqa: SLF001
            conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._attempts: Dict[str, List[float]] = {}

    # ------------------------------------------------------------------ 账号
    def create_user(self, username: str, password: str, role: str = "user",
                    note: str = "") -> Dict[str, Any]:
        username = (username or "").strip()
        if not username or len(username) > 32:
            raise ValueError("用户名需要 1~32 个字符")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        if role not in ROLES:
            raise ValueError("角色只能是 admin 或 user")
        if self.get_user(username):
            raise ValueError("用户名已存在")

        row = {
            "username": username, "password": hash_password(password), "role": role,
            "sub_key": secrets.token_hex(12), "note": note[:100],
            "created_at": int(time.time()),
        }
        with self.store._write_lock:            # noqa: SLF001
            conn = self.store._connect()        # noqa: SLF001
            with conn:
                conn.execute(
                    """INSERT INTO users (username, password, role, sub_key, note, created_at)
                       VALUES (:username, :password, :role, :sub_key, :note, :created_at)""",
                    row)
        log.info("已创建账号 %s（%s）", username, role)
        return self.get_user(username)

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        cur = self.store._connect().execute(    # noqa: SLF001
            "SELECT * FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        return dict(row) if row else None

    def user_by_key(self, sub_key: str) -> Optional[Dict[str, Any]]:
        if not sub_key or len(sub_key) < 8:
            return None
        cur = self.store._connect().execute(    # noqa: SLF001
            "SELECT * FROM users WHERE sub_key = ? AND disabled = 0", (sub_key,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        rows = self.store._connect().execute(   # noqa: SLF001
            "SELECT * FROM users ORDER BY role, username")
        return [{k: v for k, v in dict(row).items() if k != "password"} for row in rows]

    def count(self) -> int:
        return self.store._connect().execute(   # noqa: SLF001
            "SELECT COUNT(*) FROM users").fetchone()[0]

    def set_password(self, username: str, password: str) -> bool:
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        return self._update(username, password=hash_password(password))

    def set_role(self, username: str, role: str) -> bool:
        if role not in ROLES:
            raise ValueError("角色只能是 admin 或 user")
        return self._update(username, role=role)

    def set_disabled(self, username: str, disabled: bool) -> bool:
        if disabled:
            self.destroy_user_sessions(username)
        return self._update(username, disabled=1 if disabled else 0)

    def regenerate_key(self, username: str) -> str:
        key = secrets.token_hex(12)
        self._update(username, sub_key=key)
        return key

    def delete_user(self, username: str) -> bool:
        self.destroy_user_sessions(username)
        with self.store._write_lock:            # noqa: SLF001
            conn = self.store._connect()        # noqa: SLF001
            with conn:
                return conn.execute("DELETE FROM users WHERE username = ?",
                                    (username,)).rowcount > 0

    def _update(self, username: str, **fields) -> bool:
        if not fields:
            return False
        sets = ", ".join("%s = ?" % key for key in fields)
        values = list(fields.values()) + [username]
        with self.store._write_lock:            # noqa: SLF001
            conn = self.store._connect()        # noqa: SLF001
            with conn:
                return conn.execute("UPDATE users SET %s WHERE username = ?" % sets,
                                    values).rowcount > 0

    # ------------------------------------------------------------------ 登录
    def _throttled(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._attempts.get(ip, []) if now - t < ATTEMPT_WINDOW]
            self._attempts[ip] = hits
            return len(hits) >= MAX_ATTEMPTS

    def _note_failure(self, ip: str) -> None:
        with self._lock:
            self._attempts.setdefault(ip, []).append(time.time())

    def authenticate(self, username: str, password: str, ip: str = "") -> Optional[Dict[str, Any]]:
        if self._throttled(ip):
            raise PermissionError("尝试过于频繁，请 10 分钟后再试")
        user = self.get_user((username or "").strip())
        if not user or user.get("disabled"):
            self._note_failure(ip)
            return None
        if not verify_password(user["password"], password or ""):
            self._note_failure(ip)
            return None
        self._update(user["username"], last_login=int(time.time()), last_ip=ip)
        return user

    # ------------------------------------------------------------------ 会话
    def create_session(self, username: str, ip: str = "", device: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.store._write_lock:            # noqa: SLF001
            conn = self.store._connect()        # noqa: SLF001
            with conn:
                conn.execute(
                    """INSERT INTO sessions (token, username, created_at, expires_at, ip, device)
                       VALUES (?,?,?,?,?,?)""",
                    (token, username, now, now + self.session_days * 86400, ip, device[:60]))
        return token

    def session_user(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        cur = self.store._connect().execute(    # noqa: SLF001
            """SELECT u.* FROM sessions s JOIN users u ON u.username = s.username
               WHERE s.token = ? AND s.expires_at > ? AND u.disabled = 0""",
            (token, int(time.time())))
        row = cur.fetchone()
        return dict(row) if row else None

    def destroy_session(self, token: str) -> None:
        if not token:
            return
        with self.store._write_lock:            # noqa: SLF001
            conn = self.store._connect()        # noqa: SLF001
            with conn:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def destroy_user_sessions(self, username: str) -> int:
        with self.store._write_lock:            # noqa: SLF001
            conn = self.store._connect()        # noqa: SLF001
            with conn:
                return conn.execute("DELETE FROM sessions WHERE username = ?",
                                    (username,)).rowcount

    def purge_expired(self) -> int:
        with self.store._write_lock:            # noqa: SLF001
            conn = self.store._connect()        # noqa: SLF001
            with conn:
                return conn.execute("DELETE FROM sessions WHERE expires_at < ?",
                                    (int(time.time()),)).rowcount

    def active_sessions(self, username: str = "") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sessions WHERE expires_at > ?"
        params: List[Any] = [int(time.time())]
        if username:
            sql += " AND username = ?"
            params.append(username)
        rows = self.store._connect().execute(   # noqa: SLF001
            sql + " ORDER BY created_at DESC LIMIT 100", params)
        return [{k: v for k, v in dict(row).items() if k != "token"} for row in rows]

    @staticmethod
    def public(user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """给前端看的用户信息，不含密码哈希。"""
        if not user:
            return None
        return {
            "username": user["username"], "role": user["role"],
            "sub_key": user.get("sub_key", ""),
            "is_admin": user["role"] == "admin",
        }
