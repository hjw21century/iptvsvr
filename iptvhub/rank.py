"""综合评分与选优。

评分维度：稳定性(历史可用率) / 实测吞吐 / 画质 / 首包延迟，外加 HTTPS、IPv4、
多源收录的小幅加成。选优时强制同频道跨主机分散，避免"备用源和主源一起挂"。
"""

import json
import math
from collections import defaultdict
from typing import Any, Dict, List, Sequence

from .util import natural_key

LOGO_BASE = "https://live.fanmingming.cn/tv/{name}.png"


def bias_corrected_ewma(ewma: float, checks: int, alpha: float) -> float:
    """修正 EWMA 冷启动偏差：只测过一次且成功的源不应该只拿到 alpha 分。"""
    if checks <= 0:
        return 0.0
    denominator = 1.0 - (1.0 - alpha) ** checks
    if denominator <= 1e-9:
        return ewma
    return max(0.0, min(1.0, ewma / denominator))


def speed_score(kbps: float) -> float:
    """200kbps 起步，8Mbps 封顶，对数刻度。"""
    if kbps <= 0:
        return 0.0
    if kbps >= 8000:
        return 1.0
    return max(0.0, min(1.0, math.log10(max(kbps, 1.0) / 150.0) / math.log10(8000.0 / 150.0)))


def quality_score(resolution: str, bandwidth: int) -> float:
    height = 0
    if resolution and "x" in resolution.lower():
        try:
            height = int(resolution.lower().split("x")[1])
        except (ValueError, IndexError):
            height = 0
    if height:
        if height >= 2000:
            return 1.0
        if height >= 1080:
            return 0.95
        if height >= 720:
            return 0.8
        if height >= 576:
            return 0.62
        if height >= 480:
            return 0.5
        return 0.32
    if bandwidth:
        if bandwidth >= 6_000_000:
            return 0.95
        if bandwidth >= 3_000_000:
            return 0.85
        if bandwidth >= 1_500_000:
            return 0.7
        if bandwidth >= 800_000:
            return 0.55
        return 0.4
    return 0.55  # 未知画质给中性分，不惩罚也不奖励


def latency_score(ttfb_ms: float) -> float:
    if ttfb_ms <= 0:
        return 0.5
    if ttfb_ms <= 300:
        return 1.0
    if ttfb_ms >= 4000:
        return 0.0
    return max(0.0, 1.0 - (ttfb_ms - 300.0) / 3700.0)


def score_row(row: Dict[str, Any], weights: Dict[str, float], alpha: float) -> float:
    if not row.get("alive"):
        return 0.0

    checks = int(row.get("checks") or 0)
    stability_ewma = bias_corrected_ewma(float(row.get("ewma") or 0.0), checks, alpha)
    success_ratio = (int(row.get("successes") or 0) / checks) if checks else 0.0
    stability = 0.6 * stability_ewma + 0.4 * success_ratio

    components = (
        weights.get("stability", 0.45) * stability
        + weights.get("speed", 0.25) * speed_score(float(row.get("kbps") or 0.0))
        + weights.get("quality", 0.20) * quality_score(row.get("resolution") or "",
                                                       int(row.get("bandwidth") or 0))
        + weights.get("latency", 0.10) * latency_score(float(row.get("ttfb_ms") or 0.0))
    )

    if (row.get("scheme") or "").lower() == "https":
        components += weights.get("https_bonus", 0.03)
    if int(row.get("ip_version") or 0) == 4:
        components += weights.get("ipv4_bonus", 0.02)

    # 被多个上游同时收录 => 更可信
    try:
        source_count = len(json.loads(row.get("sources") or "[]"))
    except (ValueError, TypeError):
        source_count = 1
    components *= min(1.12, 1.0 + 0.04 * max(0, source_count - 1))
    components *= max(0.6, min(1.15, float(row.get("source_weight") or 1.0)))

    # 连续失败尚未清理的源降权
    fail_streak = int(row.get("fail_streak") or 0)
    if fail_streak:
        components *= max(0.4, 1.0 - 0.12 * fail_streak)

    return round(max(0.0, min(1.0, components)), 4)


def score_all(rows: Sequence[Dict[str, Any]], weights: Dict[str, float],
              alpha: float) -> Dict[str, float]:
    return {row["url"]: score_row(row, weights, alpha) for row in rows}


def pick_best(rows: Sequence[Dict[str, Any]], cfg: Dict[str, Any],
              classifier=None) -> List[Dict[str, Any]]:
    """把打过分的源聚合成频道列表（含备用源），按分组与频道名排序。"""
    max_backups = int(cfg.get("max_backups_per_channel", 3))
    max_per_host = int(cfg.get("max_per_host_per_channel", 1))
    min_score = float(cfg.get("min_score", 0.15))

    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("alive"):
            continue
        if float(row.get("score") or 0.0) < min_score:
            continue
        buckets[row["channel_key"]].append(row)

    channels: List[Dict[str, Any]] = []
    for key, entries in buckets.items():
        entries.sort(key=lambda r: (-float(r.get("score") or 0), float(r.get("ttfb_ms") or 9e9)))

        chosen: List[Dict[str, Any]] = []
        host_count: Dict[str, int] = defaultdict(int)
        for entry in entries:
            host = entry.get("host") or ""
            if host_count[host] >= max_per_host and len(chosen) > 0:
                continue
            chosen.append(entry)
            host_count[host] += 1
            if len(chosen) >= max_backups + 1:
                break
        # 如果跨主机不够，用同主机的补齐备用位
        if len(chosen) < max_backups + 1:
            for entry in entries:
                if entry in chosen:
                    continue
                chosen.append(entry)
                if len(chosen) >= max_backups + 1:
                    break

        best = chosen[0]
        display = _pick_display(entries)
        group = _pick_group(entries)
        logo = next((e.get("logo") for e in entries if e.get("logo")), "") or \
            LOGO_BASE.format(name=display)

        channels.append({
            "key": key,
            "name": display,
            "group": group,
            "logo": logo,
            "score": float(best.get("score") or 0),
            "url": best["url"],
            "kbps": round(float(best.get("kbps") or 0), 1),
            "ttfb_ms": round(float(best.get("ttfb_ms") or 0), 1),
            "resolution": best.get("resolution") or "",
            "kind": best.get("kind") or "",
            "ip_version": int(best.get("ip_version") or 0),
            "uptime": round(_uptime(best), 3),
            "sources": _sources(best),
            "streams": [{
                "url": e["url"],
                "score": float(e.get("score") or 0),
                "kbps": round(float(e.get("kbps") or 0), 1),
                "ttfb_ms": round(float(e.get("ttfb_ms") or 0), 1),
                "resolution": e.get("resolution") or "",
                "ip_version": int(e.get("ip_version") or 0),
                "uptime": round(_uptime(e), 3),
                "host": e.get("host") or "",
            } for e in chosen],
        })

    rank = classifier.group_rank if classifier else (lambda g: 0)
    channels.sort(key=lambda c: (rank(c["group"]), natural_key(c["name"])))
    return channels


def _uptime(row: Dict[str, Any]) -> float:
    checks = int(row.get("checks") or 0)
    if not checks:
        return 0.0
    return (int(row.get("successes") or 0)) / checks


def _sources(row: Dict[str, Any]) -> List[str]:
    try:
        return json.loads(row.get("sources") or "[]")
    except (ValueError, TypeError):
        return []


def _pick_display(entries: Sequence[Dict[str, Any]]) -> str:
    counter: Dict[str, int] = defaultdict(int)
    for entry in entries:
        counter[entry["display_name"]] += 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]


def _pick_group(entries: Sequence[Dict[str, Any]]) -> str:
    counter: Dict[str, int] = defaultdict(int)
    for entry in entries:
        counter[entry.get("group_title") or "其他频道"] += 1
    ranked = sorted(counter.items(), key=lambda kv: -kv[1])
    for group, _ in ranked:
        if group != "其他频道":
            return group
    return ranked[0][0]
