"""进程内运行时状态：更新进度 + 日志环形缓冲。

管理后台要看"现在跑到哪了"和"刚才报了什么错"，这些信息不适合落库
（高频、易失、只对当前进程有意义），放在内存里由 HTTP 接口读取。
"""

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List


class RunState:
    """当前更新任务的进度。线程安全，pipeline 写、HTTP 读。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "running": False,
            "phase": "idle",       # idle / collect / probe / export / done / error
            "message": "",
            "done": 0,
            "total": 0,
            "alive": 0,
            "started_at": 0,
            "finished_at": 0,
            "trigger": "",
        }

    def begin(self, trigger: str = "manual") -> None:
        with self._lock:
            self._data.update({
                "running": True, "phase": "collect", "message": "正在拉取上游清单",
                "done": 0, "total": 0, "alive": 0,
                "started_at": int(time.time()), "finished_at": 0, "trigger": trigger,
            })

    def update(self, **fields) -> None:
        with self._lock:
            self._data.update(fields)

    def progress(self, done: int, total: int, alive: int) -> None:
        with self._lock:
            self._data.update({"done": done, "total": total, "alive": alive})

    def finish(self, phase: str = "done", message: str = "") -> None:
        with self._lock:
            self._data.update({
                "running": False, "phase": phase, "message": message,
                "finished_at": int(time.time()),
            })

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._data)
        if data["total"]:
            data["percent"] = round(100.0 * data["done"] / data["total"], 1)
        else:
            data["percent"] = 0.0
        if data["started_at"]:
            end = data["finished_at"] or int(time.time())
            data["elapsed"] = end - data["started_at"]
        else:
            data["elapsed"] = 0
        return data


class LogRing(logging.Handler):
    """把最近若干条日志留在内存里，供后台页面展示。"""

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.records: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        with self._lock:
            self._seq += 1
            self.records.append({
                "seq": self._seq,
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "message": message,
            })

    def tail(self, after: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            items = [r for r in self.records if r["seq"] > after]
        return items[-limit:]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq


RUN_STATE = RunState()
LOG_RING = LogRing()


def attach_log_ring(level: int = logging.INFO) -> LogRing:
    root = logging.getLogger()
    if LOG_RING not in root.handlers:
        LOG_RING.setLevel(level)
        root.addHandler(LOG_RING)
    return LOG_RING
