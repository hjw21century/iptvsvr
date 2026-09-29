"""站内公告 / 节日祝福。

按日期区间自动生效与失效——节后不需要谁记得回来把横幅撤掉。
"""

import datetime
import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger("iptvhub.notices")

STYLES = ("festive", "info", "warn")


def _parse_date(value: Any) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date(*(int(part) for part in str(value).split("-")[:3]))
    except (ValueError, TypeError):
        return None


def pick_active(notices: List[Dict[str, Any]],
                today: Optional[datetime.date] = None) -> Optional[Dict[str, Any]]:
    today = today or datetime.date.today()
    candidates = []
    for item in notices or []:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        start = _parse_date(item.get("start"))
        end = _parse_date(item.get("end"))
        if start and today < start:
            continue
        if end and today > end:
            continue
        candidates.append(item)

    if not candidates:
        return None
    candidates.sort(key=lambda item: float(item.get("priority", 0)), reverse=True)
    chosen = dict(candidates[0])
    if chosen.get("style") not in STYLES:
        chosen["style"] = "info"
    chosen["lines"] = [str(line) for line in (chosen.get("lines") or [])][:6]
    return chosen


def load_active(config_dir: str, today: Optional[datetime.date] = None) -> Optional[Dict[str, Any]]:
    import json
    path = os.path.join(config_dir, "notices.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (ValueError, OSError) as exc:
        log.warning("公告配置读取失败: %s", exc)
        return None
    return pick_active(payload.get("notices", []), today)
