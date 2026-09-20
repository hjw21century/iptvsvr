"""M3U / TXT 播放列表解析。

相比常见实现额外处理：
  * TXT 的 `分类名,#genre#` 分组行（上游分组信息不再丢失）
  * 一行多源 `频道名,url1#url2#url3`
  * `#EXTGRP:` 分组、`tvg-*` 属性、单双引号属性
  * 公告/广告条目过滤
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .util import contains_date, normalize_key

_ATTR_RE = re.compile(r'([A-Za-z0-9\-_]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))')
_SUPPORTED_SCHEMES = ("http://", "https://")


@dataclass
class RawEntry:
    name: str
    url: str
    logo: str = ""
    group: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    source: str = ""
    weight: float = 1.0
    attrs: Dict[str, str] = field(default_factory=dict)


def _parse_attrs(extinf: str) -> Dict[str, str]:
    head = extinf.split(",", 1)[0]
    attrs = {}
    for match in _ATTR_RE.finditer(head):
        key = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attrs[key] = value.strip()
    return attrs


def _split_urls(raw: str) -> List[str]:
    """`url1#url2` 是国内 TXT 清单的多源写法；普通带 fragment 的 URL 不受影响。"""
    raw = raw.strip()
    if "#" not in raw:
        return [raw] if raw else []
    parts = [p.strip() for p in raw.split("#") if p.strip()]
    if len(parts) > 1 and all(p.lower().startswith(_SUPPORTED_SCHEMES) for p in parts):
        return parts
    return [raw]


def is_supported_url(url: str) -> bool:
    return url.lower().startswith(_SUPPORTED_SCHEMES)


class NoiseFilter:
    """过滤上游清单里的公告、广告、打赏提示等伪频道。"""

    def __init__(self, blocked_keywords: Iterable[str]):
        self.blocked = tuple(normalize_key(k) for k in blocked_keywords if k)

    def is_noise(self, name: str, group: str = "") -> bool:
        if not name:
            return True
        key = normalize_key(name)
        if not key or len(key) > 40:
            return True
        if contains_date(name):
            return True
        # 纯数字 / 纯符号
        if key.isdigit():
            return True
        for keyword in self.blocked:
            if keyword and keyword in key:
                return True
        group_key = normalize_key(group)
        for keyword in self.blocked:
            if keyword and len(keyword) >= 4 and keyword in group_key:
                return True
        return False


def parse_m3u(content: str, source: str = "", weight: float = 1.0,
              noise: Optional[NoiseFilter] = None) -> List[RawEntry]:
    entries: List[RawEntry] = []
    name = ""
    attrs: Dict[str, str] = {}
    group_hint = ""

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.upper().startswith("#EXTINF"):
            attrs = _parse_attrs(line)
            parts = line.split(",", 1)
            name = parts[1].strip() if len(parts) > 1 else attrs.get("tvg-name", "")
            continue

        if line.upper().startswith("#EXTGRP:"):
            group_hint = line.split(":", 1)[1].strip()
            continue

        if line.startswith("#"):
            continue

        if not is_supported_url(line):
            name, attrs = "", {}
            continue

        for url in _split_urls(line):
            if not is_supported_url(url):
                continue
            group = attrs.get("group-title") or attrs.get("tvg-group") or group_hint
            channel = name or attrs.get("tvg-name") or ""
            if noise and noise.is_noise(channel, group):
                continue
            entries.append(RawEntry(
                name=channel,
                url=url,
                logo=attrs.get("tvg-logo", ""),
                group=group,
                tvg_id=attrs.get("tvg-id", ""),
                tvg_name=attrs.get("tvg-name", ""),
                source=source,
                weight=weight,
                attrs=attrs,
            ))
        name, attrs = "", {}

    return entries


def parse_txt(content: str, source: str = "", weight: float = 1.0,
              noise: Optional[NoiseFilter] = None) -> List[RawEntry]:
    entries: List[RawEntry] = []
    group_hint = ""

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue

        name, _, rest = line.partition(",")
        name = name.strip()
        rest = rest.strip()

        # 分组行：`央视频道,#genre#`
        if rest.lower().startswith("#genre#") or rest.lower() == "#genre#":
            group_hint = name
            continue

        if not is_supported_url(rest) and "#" not in rest:
            continue

        for url in _split_urls(rest):
            if not is_supported_url(url):
                continue
            if noise and noise.is_noise(name, group_hint):
                continue
            entries.append(RawEntry(
                name=name,
                url=url,
                group=group_hint,
                source=source,
                weight=weight,
            ))

    return entries


def parse_playlist(content: str, kind: str = "auto", url: str = "", source: str = "",
                   weight: float = 1.0, noise: Optional[NoiseFilter] = None) -> List[RawEntry]:
    kind = (kind or "auto").lower()
    if kind == "auto":
        lowered = url.lower()
        if lowered.endswith((".m3u", ".m3u8")):
            kind = "m3u"
        elif lowered.endswith(".txt"):
            kind = "txt"
        else:
            kind = "m3u" if content.lstrip().upper().startswith("#EXTM3U") else "txt"
    if kind == "m3u":
        parsed = parse_m3u(content, source, weight, noise)
        # 某些源扩展名写成 .m3u 实际是 TXT 格式，兜底再解析一次
        if not parsed and "," in content:
            parsed = parse_txt(content, source, weight, noise)
        return parsed
    parsed = parse_txt(content, source, weight, noise)
    if not parsed and content.lstrip().upper().startswith("#EXTM3U"):
        parsed = parse_m3u(content, source, weight, noise)
    return parsed
