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

    # 登录认证
    #   require_login       前台是否必须登录才能浏览（默认否，首页公开）
    #   proxy_require_login 网页内"中转播放"是否需要登录（默认是）——
    #                       直连源由浏览器自己去拉，不花本站带宽，游客也能看；
    #                       中转要消耗本站流量，所以留给登录用户
    "require_login": False,
    "proxy_require_login": True,
    "session_days": 14,

    # 网页内播放用的中转：浏览器有混合内容与跨域限制，必须经本站转一道。
    # 中转会放大流量（一个人看 1 小时 1080p≈1.5GB 出站），所以三层设闸。
    "proxy_enabled": True,
    "proxy_max_concurrent": 6,          # 全站同时中转的请求数
    "proxy_daily_gb": 10,               # 全站每日中转流量上限（GB），0=不限
    "proxy_per_ip_daily_mb": 1200,      # 单个访客每日上限（MB），约 1 小时高清
    "proxy_per_ip_concurrent": 2,       # 单个访客同时预览的路数
    "proxy_max_request_mb": 200,        # 单次请求最多中转多少（挡住无限长的裸流）
    "proxy_max_request_seconds": 300,   # 单次请求最长持续时间

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
        "direct_bonus": 0.03,   # 浏览器可直连（https + CORS），不占本站中转带宽
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


# --------------------------------------------------------------------- 后台读写
# 管理后台需要读写配置文件原文（含 _comment 等注释键），因此与上面的"加载合并后
# 的运行配置"分开。写入一律先落临时文件再 os.replace，避免写坏配置。

def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def config_file_path(path: str = None) -> str:
    return path or os.environ.get("IPTVHUB_CONFIG") or os.path.join(CONFIG_DIR, "config.json")


def sources_file_path(path: str = None) -> str:
    return path or os.path.join(CONFIG_DIR, "sources.json")


def load_config_file(path: str = None) -> Dict[str, Any]:
    """读取 config.json 原文（不与默认值合并）。"""
    return _load_json(config_file_path(path))


def save_config_file(payload: Dict[str, Any], path: str = None) -> str:
    target = config_file_path(path)
    _atomic_write_json(target, payload)
    return target


def load_sources_raw(path: str = None) -> list:
    """读取 sources.json 中的全部源（含被停用的），供后台展示与编辑。"""
    payload = _load_json(sources_file_path(path))
    items = []
    for item in payload.get("sources", []):
        items.append({
            "name": item.get("name") or item.get("url", ""),
            "url": item.get("url", ""),
            "type": (item.get("type") or "auto").lower(),
            "enabled": bool(item.get("enabled", True)),
            "weight": float(item.get("weight", 1.0)),
            "note": item.get("note", ""),
        })
    return items


def save_sources(sources: list, path: str = None) -> str:
    target = sources_file_path(path)
    existing = _load_json(target)
    payload = {
        "_comment": existing.get(
            "_comment",
            "上游公开源清单。type: m3u | txt | auto(按扩展名推断)。"
            "enabled=false 可临时停用；note 说明停用原因。weight 影响评分中的来源加权。"),
        "sources": sources,
    }
    _atomic_write_json(target, payload)
    return target
