"""频道名规范化与分组归类。

两件事：
  1. canonical()  把 "CCTV-1 综合 高清" / "cctv1HD" 归一到同一个频道身份，用于跨源去重合并。
  2. group_of()   给频道分配展示分组（央视/卫视/港澳台/省市/主题/海外/其他）。
"""

import re
from typing import Dict, List, Optional, Tuple

from .util import half_width, normalize_cctv, normalize_key, strip_quality_markers, to_simplified

_CCTV_NUM_RE = re.compile(r"(?i)\bCCTV[\s\-_]*(4K|8K|\d{1,2}\+?)(?![0-9])")
_SATELLITE_RE = re.compile(r"([一-龥A-Za-z]{2,8}卫视)")
_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")
CCTV_REGIONS = ("欧洲", "美洲", "亚洲")


class Classifier:
    def __init__(self, groups_cfg: dict):
        cfg = groups_cfg or {}
        self.group_order: List[str] = cfg.get("group_order", [])
        self.cctv_prefixes = tuple(normalize_key(p) for p in cfg.get("cctv_prefixes", []))
        self.cctv_names = {normalize_key(normalize_cctv(n)) for n in cfg.get("cctv_names", [])}
        self.hmt = self._sorted_keys(cfg.get("hmt_keywords", []))
        self.platform_prefixes = self._sorted_keys(cfg.get("live_platform_prefixes", []))
        self.satellite_markers = self._sorted_keys(cfg.get("satellite_markers", ["卫视"])) or ["卫视"]
        self.overseas = self._sorted_keys(cfg.get("overseas_keywords", []))
        self.name_aliases = {normalize_key(k): v for k, v in (cfg.get("name_aliases") or {}).items()}

        # 地名 token -> 分组名，长 token 优先匹配
        self.geo_tokens: List[Tuple[str, str]] = []
        for province, meta in (cfg.get("provinces") or {}).items():
            group = "%s频道" % province
            tokens = {province}
            tokens.update(meta.get("aliases", []))
            tokens.update(meta.get("cities", []))
            for token in tokens:
                key = normalize_key(token)
                if len(key) >= 2:
                    self.geo_tokens.append((key, group))
        self.geo_tokens.sort(key=lambda item: len(item[0]), reverse=True)

        self.theme_tokens: List[Tuple[str, str]] = []
        for theme, keywords in (cfg.get("theme_keywords") or {}).items():
            for keyword in keywords:
                key = normalize_key(keyword)
                if key:
                    self.theme_tokens.append((key, theme))
        self.theme_tokens.sort(key=lambda item: len(item[0]), reverse=True)

        self._group_lookup = {normalize_key(g): g for g in self.group_order}
        self._cache: Dict[str, Tuple[str, str]] = {}

    @staticmethod
    def _hit(key: str, token: str) -> bool:
        """短 ASCII 关键词只做精确匹配。

        否则 "AM" 会命中 "Asian DrAMa"、"RT" 会命中 "SpoRTs"，
        中文词保持子串匹配（"浙江" 需要能命中 "浙江民生休闲"）。
        """
        if token.isascii() and len(token) <= 3:
            return key == token
        return token in key

    @staticmethod
    def _sorted_keys(values) -> List[str]:
        keys = [normalize_key(v) for v in values if normalize_key(v)]
        return sorted(set(keys), key=len, reverse=True)

    # ------------------------------------------------------------ 频道身份
    def canonical(self, raw_name: str) -> Tuple[str, str]:
        """返回 (展示名, 身份键)。身份键相同的条目视为同一个频道。"""
        if raw_name in self._cache:
            return self._cache[raw_name]

        name = half_width(to_simplified(raw_name or "")).strip()
        name = re.sub(r"\s+", " ", name)

        # CCTV 先于画质清洗处理，避免 "CCTV-4K" 里的 4K 被当成画质标记删掉
        match = _CCTV_NUM_RE.search(name)
        if match:
            number = match.group(1).upper()
            tail = name[match.end():]
            region = next((r for r in CCTV_REGIONS if r in tail), "")
            display = "CCTV%s%s" % (number, region)
            result = (display, normalize_key(display))
            self._cache[raw_name] = result
            return result

        display = strip_quality_markers(name) or name
        display = normalize_cctv(display).strip(" -_|·,")
        key = normalize_key(display)

        if key in self.name_aliases:
            display = self.name_aliases[key]
            key = normalize_key(display)
        else:
            sat = _SATELLITE_RE.search(display)
            if sat:
                display = sat.group(1)
                key = normalize_key(display)

        if not key:
            display, key = (raw_name or "").strip(), normalize_key(raw_name)

        result = (display, key)
        self._cache[raw_name] = result
        return result

    # -------------------------------------------------------------- 分组
    def _upstream_group(self, upstream: str) -> Optional[str]:
        if not upstream:
            return None
        key = normalize_key(upstream)
        if key in self._group_lookup:
            return self._group_lookup[key]
        for token, group in self.geo_tokens:
            if self._hit(key, token):
                return group
        for token, theme in self.theme_tokens:
            if len(token) >= 4 and self._hit(key, token):
                return theme
        return None

    def group_of(self, display_name: str, upstream_group: str = "") -> str:
        key = normalize_key(normalize_cctv(display_name))

        # 1. 央视
        if _CCTV_NUM_RE.search(display_name) or key in self.cctv_names:
            return "央视频道"
        for prefix in self.cctv_prefixes:
            if prefix and key.startswith(prefix):
                return "央视频道"

        # 2. 直播平台的游戏间（「B站」「斗鱼」开头），整段归为游戏电竞，
        #    否则里面的地名/关键词会把它们误分到省市频道
        for prefix in self.platform_prefixes:
            if prefix and key.startswith(prefix):
                return "游戏电竞"

        # 3. 港澳台（先于卫视：凤凰卫视 / 莲花卫视 属于港澳台）
        for token in self.hmt:
            if token and self._hit(key, token):
                return "港澳台频道"

        # 4. 卫视
        for marker in self.satellite_markers:
            if marker in key:
                return "卫视频道"

        # 5. 省市（地名优先于语言与主题：深圳体育 → 广东频道）
        for token, group in self.geo_tokens:
            if self._hit(key, token):
                return group

        # 6. 海外关键词 + 纯英文频道
        #    放在主题之前：BBC News / RT News 归"海外"比归"新闻"更好找
        for token in self.overseas:
            if token and self._hit(key, token):
                return "海外频道"
        if _ASCII_RE.match(key or "中") and len(key) >= 3:
            return "海外频道"

        # 7. 主题
        for token, theme in self.theme_tokens:
            if self._hit(key, token):
                return theme

        # 8. 参考上游分组
        upstream_match = self._upstream_group(upstream_group)
        if upstream_match:
            return upstream_match

        return "其他频道"

    def group_rank(self, group: str) -> int:
        try:
            return self.group_order.index(group)
        except ValueError:
            return len(self.group_order)
