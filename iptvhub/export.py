"""播放列表导出（M3U / M3U8 / TXT / JSON）。

同一份数据可导出多种视图：只含最优源的精简列表、含备用源的完整列表、
以及按分组/画质/IP 版本过滤后的动态列表（由 HTTP 接口调用）。
"""

import json
import os
from typing import Any, Dict, Iterable, List, Optional

from .util import human_time, normalize_key


def _resolution_height(resolution: str) -> int:
    if resolution and "x" in resolution.lower():
        try:
            return int(resolution.lower().split("x")[1])
        except (ValueError, IndexError):
            return 0
    return 0


def filter_channels(channels: List[Dict[str, Any]], group: str = "", query: str = "",
                    ip_version: int = 0, min_score: float = 0.0,
                    min_height: int = 0, limit: int = 0) -> List[Dict[str, Any]]:
    result = []
    query_key = normalize_key(query) if query else ""
    group_key = normalize_key(group) if group else ""

    for channel in channels:
        if group_key and normalize_key(channel.get("group", "")) != group_key:
            continue
        if query_key and query_key not in normalize_key(channel.get("name", "")):
            continue
        if min_score and float(channel.get("score") or 0) < min_score:
            continue
        if min_height and _resolution_height(channel.get("resolution", "")) < min_height:
            continue
        if ip_version:
            streams = [s for s in channel.get("streams", [])
                       if int(s.get("ip_version") or 0) == ip_version]
            if not streams:
                continue
            channel = dict(channel, streams=streams, url=streams[0]["url"])
        result.append(channel)
        if limit and len(result) >= limit:
            break
    return result


def render_m3u(channels: Iterable[Dict[str, Any]], include_backups: bool = False,
               epg_url: str = "", generated_at: str = "", extra_header: str = "",
               site_url: str = "") -> str:
    lines = []
    header = "#EXTM3U"
    if epg_url:
        header += ' x-tvg-url="%s"' % epg_url
    lines.append(header)
    lines.append("# Generated-By: IPTV-Hub")
    lines.append("# Generated-Time: %s" % (generated_at or human_time()))
    if site_url:
        lines.append("# Source: %s" % site_url)
    if extra_header:
        lines.append("# %s" % extra_header)

    count = 0
    for channel in channels:
        streams = channel.get("streams") or [{"url": channel.get("url", "")}]
        if not include_backups:
            streams = streams[:1]
        for index, stream in enumerate(streams):
            url = stream.get("url")
            if not url:
                continue
            name = channel["name"] if index == 0 else "%s [备%d]" % (channel["name"], index)
            attrs = [
                'tvg-id="%s"' % channel.get("key", ""),
                'tvg-name="%s"' % channel["name"],
                'tvg-logo="%s"' % channel.get("logo", ""),
                'group-title="%s"' % channel.get("group", "其他频道"),
            ]
            resolution = stream.get("resolution") or channel.get("resolution") or ""
            if resolution:
                attrs.append('tvg-resolution="%s"' % resolution)
            lines.append("#EXTINF:-1 %s,%s" % (" ".join(attrs), name))
            lines.append(url)
            count += 1

    lines.insert(3, "# Channel-Count: %d" % count)
    return "\n".join(lines) + "\n"


def render_txt(channels: Iterable[Dict[str, Any]], include_backups: bool = False) -> str:
    grouped: Dict[str, List[str]] = {}
    order: List[str] = []
    for channel in channels:
        group = channel.get("group", "其他频道")
        if group not in grouped:
            grouped[group] = []
            order.append(group)
        streams = channel.get("streams") or [{"url": channel.get("url", "")}]
        if not include_backups:
            streams = streams[:1]
        urls = [s["url"] for s in streams if s.get("url")]
        if urls:
            grouped[group].append("%s,%s" % (channel["name"], "#".join(urls)))

    lines = []
    for group in order:
        lines.append("%s,#genre#" % group)
        lines.extend(grouped[group])
        lines.append("")
    return "\n".join(lines)


def render_json(channels: List[Dict[str, Any]], stats: Optional[Dict[str, Any]] = None,
                generated_at: str = "") -> str:
    payload = {
        "generated_at": generated_at or human_time(),
        "channel_count": len(channels),
        "groups": sorted({c.get("group", "其他频道") for c in channels}),
        "stats": stats or {},
        "channels": channels,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def write_outputs(data_dir: str, channels: List[Dict[str, Any]],
                  stats: Optional[Dict[str, Any]] = None, epg_url: str = "",
                  site_url: str = "") -> Dict[str, str]:
    os.makedirs(data_dir, exist_ok=True)
    generated_at = human_time()
    files = {
        "playlist.m3u": render_m3u(channels, False, epg_url, generated_at, site_url=site_url),
        "playlist.m3u8": render_m3u(channels, False, epg_url, generated_at, site_url=site_url),
        "playlist_full.m3u": render_m3u(channels, True, epg_url, generated_at,
                                        extra_header="含备用源", site_url=site_url),
        "playlist.txt": render_txt(channels, False),
        "channels.json": render_json(channels, stats, generated_at),
    }
    written = {}
    for name, content in files.items():
        path = os.path.join(data_dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)  # 原子替换，避免服务读到半截文件
        written[name] = path
    return written
