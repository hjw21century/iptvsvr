"""运行配置加载。

配置来源优先级：环境变量 IPTVHUB_CONFIG 指定的文件 > config/config.json > 内置默认值。
"""

import json
import os
from typing import Any, Dict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.environ.get("IPTVHUB_DATA", os.path.join(BASE_DIR, "data"))
WEB_DIR = os.path.join(BASE_DIR, "web")

DEFAULTS: Dict[str, Any] = {
    # 对外访问地址，用于播放列表头部标注来源；留空则不输出
    "site_url": "",

    # 播放器用的 EPG（节目单）地址，会写进 M3U 头部 x-tvg-url
    "epg_url": "https://live.fanmingming.cn/e.xml",

    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",

    # 上游清单抓取
    "fetch_timeout": 15,
    "fetch_max_bytes": 12 * 1024 * 1024,

    # 探测
    "concurrency": 48,              # 全局并发探测数
    "per_host_concurrency": 4,      # 单主机并发上限（避免压垮上游）
    "connect_timeout": 6,           # 连接/首字节超时（秒）
    "probe_timeout": 14,            # 单条链接总耗时上限（秒）
    "probe_bytes": 256 * 1024,      # 媒体分片最多读取字节数
    "probe_seconds": 4.0,           # 媒体分片最多读取时长（秒）
    "min_bytes": 16 * 1024,         # 判定"有码流"的最小字节数
    "manifest_max_bytes": 1024 * 1024,
    "probe_retries": 0,             # 失败重试次数
    "host_failure_limit": 8,        # 单主机连续连接级失败多少次后熔断（0 = 关闭）

    # 历史与评分
    "ewma_alpha": 0.4,              # 可用率指数滑动平均系数
    "prune_after_days": 7,          # 超过该天数未成功则清理
    "prune_fail_streak": 8,         # 连续失败达到该值则清理
    "recheck_dead_after_hours": 12, # 已知失效链接的重测冷却时间

    # 选优
    "max_backups_per_channel": 3,   # 每个频道最多保留的备用源数量
    "max_per_host_per_channel": 1,  # 同一频道同一主机最多保留几条（保证容灾分散）
    "min_score": 0.15,              # 低于该综合评分的源不进入播放列表

    "weights": {
        "stability": 0.45,
        "speed": 0.25,
        "quality": 0.20,
        "latency": 0.10,
        "https_bonus": 0.03,
        "ipv4_bonus": 0.02,
    },

    "server": {
        "host": "0.0.0.0",
        "port": 8088,
        "admin_token": "",
        "auto_update": True,
        "update_interval_hours": 4,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_config(path: str = None) -> Dict[str, Any]:
    path = path or os.environ.get("IPTVHUB_CONFIG") or os.path.join(CONFIG_DIR, "config.json")
    cfg = _deep_merge(DEFAULTS, _load_json(path))
    cfg["paths"] = {
        "base": BASE_DIR,
        "config": CONFIG_DIR,
        "data": DATA_DIR,
        "web": WEB_DIR,
        "db": os.path.join(DATA_DIR, "iptv.db"),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    return cfg


def load_sources(path: str = None):
    """返回启用的上游源列表 [{name, url, type, weight}]。"""
    path = path or os.path.join(CONFIG_DIR, "sources.json")
    payload = _load_json(path)
    sources = []
    for item in payload.get("sources", []):
        if not item.get("enabled", True):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        sources.append({
            "name": item.get("name") or url,
            "url": url,
            "type": (item.get("type") or "auto").lower(),
            "weight": float(item.get("weight", 1.0)),
        })
    return sources


def load_groups(path: str = None) -> Dict[str, Any]:
    path = path or os.path.join(CONFIG_DIR, "groups.json")
    return _load_json(path)
