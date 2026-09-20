"""文本归一化、排序键与通用小工具。"""

import logging
import re
import sys
import time
from typing import Any, Tuple

# 繁体 -> 简体（只覆盖频道名中的高频字，避免引入额外依赖）
TRAD_SIMP = str.maketrans({
    "頻": "频", "視": "视", "臺": "台", "綜": "综", "聞": "闻", "體": "体", "藝": "艺",
    "經": "经", "濟": "济", "娛": "娱", "樂": "乐", "電": "电", "廣": "广", "畫": "画",
    "劇": "剧", "紀": "纪", "錄": "录", "網": "网", "導": "导", "衛": "卫", "陰": "阴",
    "陽": "阳", "麗": "丽", "龍": "龙", "鄉": "乡", "鎮": "镇", "區": "区", "縣": "县",
    "灣": "湾", "滬": "沪", "閩": "闽", "贛": "赣", "蘇": "苏", "魯": "鲁", "鄂": "鄂",
    "湘": "湘", "粵": "粤", "瓊": "琼", "遼": "辽", "寧": "宁", "貴": "贵", "雲": "云",
    "陝": "陕", "晉": "晋", "錫": "锡", "訊": "讯", "資": "资", "際": "际", "國": "国",
    "際": "际", "劇": "剧", "動": "动", "漫": "漫", "兒": "儿", "少": "少", "數": "数",
    "學": "学", "農": "农", "業": "业", "軍": "军", "戲": "戏", "曲": "曲", "風": "风",
    "雨": "雨", "華": "华", "東": "东", "南": "南", "西": "西", "北": "北", "門": "门",
    "開": "开", "關": "关", "萬": "万", "馬": "马", "鳳": "凤", "凰": "凰", "無": "无",
    "線": "线", "翡": "翡", "翠": "翠", "財": "财", "産": "产", "産": "产", "縱": "纵",
    "橫": "横", "際": "际", "務": "务", "營": "营", "壽": "寿", "醫": "医", "藥": "药",
    "護": "护", "習": "习", "書": "书", "聲": "声", "義": "义", "節": "节", "慶": "庆",
    "圓": "圆", "團": "团", "園": "园", "觀": "观", "覽": "览", "遊": "游", "歷": "历",
    "現": "现", "場": "场", "轉": "转", "播": "播", "臨": "临", "億": "亿", "長": "长",
})

# 全角 -> 半角
FULLWIDTH = {c: chr(ord(c) - 0xFEE0) for c in
             "！＂＃＄％＆＇（）＊＋，－．／０１２３４５６７８９：；＜＝＞？＠"
             "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ［＼］＾＿｀"
             "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ｛｜｝～"}
FULLWIDTH["\u3000"] = " "  # 表意空格
FULLWIDTH_MAP = str.maketrans(FULLWIDTH)

_PUNCT_RE = re.compile(r"[ \t\r\n\-_|·•:：,，.。/\\()\[\]【】「」『』<>《》'\"`~!！?？*#＃]+")
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}")
_CCTV_RE = re.compile(r"(?i)\bCCTV[\s\-_]*(4K|8K|\d{1,2}\+?)(?![0-9])")
_QUALITY_RE = re.compile(
    r"(?i)[\s\-_（(\[【]*(?<![A-Za-z])(?:IPV6|IPV4|HEVC|H\.?265|H\.?264|HDR|UHD|FHD|HD|SD|BD|"
    r"\d{3,4}[Pp]|4K|8K|60FPS|高清|超清|标清|蓝光|流畅|备用\d*|线路\d*|源\d*)\b[\s\-_）)\]】]*")


def to_simplified(text: str) -> str:
    return text.translate(TRAD_SIMP)


def half_width(text: str) -> str:
    return text.translate(FULLWIDTH_MAP)


def normalize_key(text: str) -> str:
    """归一化匹配键：繁转简 + 全角转半角 + 去标点 + 大写。"""
    if not text:
        return ""
    value = half_width(to_simplified(text)).strip().upper()
    value = value.replace("＋", "+")
    return _PUNCT_RE.sub("", value)


def strip_quality_markers(text: str) -> str:
    """去掉 HD / 1080P / IPV6 / 备用1 之类的画质与线路标记。"""
    cleaned = re.sub(r"[（(【\[][^)）】\]]{0,20}[)）】\]]", " ", text)
    cleaned = _QUALITY_RE.sub(" ", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -_|·")


def normalize_cctv(text: str) -> str:
    """CCTV-1 / CCTV 1 / cctv1 -> CCTV1。"""
    return _CCTV_RE.sub(lambda m: "CCTV" + m.group(1).upper(), text)


def contains_date(text: str) -> bool:
    return bool(_DATE_RE.search(text or ""))


def natural_key(text: str) -> Tuple[Any, ...]:
    """让 CCTV2 排在 CCTV10 前面的自然排序键。"""
    parts = re.split(r"(\d+)", text or "")
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part), ""))
        elif part:
            key.append((1, 0, part))
    return tuple(key)


def now() -> int:
    return int(time.time())


def human_time(ts: float = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return time.strftime(fmt, time.localtime(ts if ts else time.time()))


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def setup_logging(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
        root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger("iptvhub")
