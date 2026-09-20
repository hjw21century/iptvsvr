"""从真实码流里解析视频分辨率（不依赖 ffprobe）。

国内绝大多数直播源是单码率的 media playlist，manifest 里没有 RESOLUTION/BANDWIDTH，
所以只能从下载到的分片里拿：解复用 MPEG-TS → 找 H.264 SPS → 解析出宽高；
fMP4 分片则直接从 avcC box 里取 SPS。解析失败一律返回空串，不影响可用性判定。
"""

from typing import Optional, Tuple

# 使用 high profile 系列时 SPS 里多出色度相关字段
_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


class _BitReader:
    """SPS 是按位打包的，需要位级读取 + 指数哥伦布解码。"""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0  # 以 bit 为单位

    def bits_left(self) -> int:
        return len(self.data) * 8 - self.pos

    def u(self, count: int) -> int:
        if count > self.bits_left():
            raise ValueError("bitstream exhausted")
        value = 0
        for _ in range(count):
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while True:
            if self.bits_left() <= 0:
                raise ValueError("bitstream exhausted")
            if self.u(1):
                break
            zeros += 1
            if zeros > 32:
                raise ValueError("invalid exp-golomb")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)


def strip_emulation_prevention(data: bytes) -> bytes:
    """去掉 H.264 的 0x000003 防竞争字节。"""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0x00 else 0
    return bytes(out)


def _skip_scaling_list(reader: _BitReader, size: int) -> None:
    last_scale = 8
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            delta = reader.se()
            next_scale = (last_scale + delta + 256) % 256
        last_scale = next_scale if next_scale != 0 else last_scale


def parse_sps(nal: bytes) -> Optional[Tuple[int, int]]:
    """解析 H.264 SPS NAL（不含起始码，含 1 字节 nal header），返回 (宽, 高)。"""
    if len(nal) < 4 or (nal[0] & 0x1F) != 7:
        return None

    payload = strip_emulation_prevention(nal[1:])
    reader = _BitReader(payload)
    try:
        profile_idc = reader.u(8)
        reader.u(8)          # constraint flags + reserved
        reader.u(8)          # level_idc
        reader.ue()          # seq_parameter_set_id

        if profile_idc in _HIGH_PROFILES:
            chroma_format_idc = reader.ue()
            if chroma_format_idc == 3:
                reader.u(1)  # separate_colour_plane_flag
            reader.ue()      # bit_depth_luma_minus8
            reader.ue()      # bit_depth_chroma_minus8
            reader.u(1)      # qpprime_y_zero_transform_bypass_flag
            if reader.u(1):  # seq_scaling_matrix_present_flag
                count = 8 if chroma_format_idc != 3 else 12
                for index in range(count):
                    if reader.u(1):
                        _skip_scaling_list(reader, 16 if index < 6 else 64)

        reader.ue()          # log2_max_frame_num_minus4
        pic_order_cnt_type = reader.ue()
        if pic_order_cnt_type == 0:
            reader.ue()      # log2_max_pic_order_cnt_lsb_minus4
        elif pic_order_cnt_type == 1:
            reader.u(1)
            reader.se()
            reader.se()
            for _ in range(reader.ue()):
                reader.se()

        reader.ue()          # max_num_ref_frames
        reader.u(1)          # gaps_in_frame_num_value_allowed_flag
        width_mbs = reader.ue() + 1
        height_map_units = reader.ue() + 1
        frame_mbs_only = reader.u(1)
        if not frame_mbs_only:
            reader.u(1)      # mb_adaptive_frame_field_flag
        reader.u(1)          # direct_8x8_inference_flag

        crop_left = crop_right = crop_top = crop_bottom = 0
        if reader.u(1):      # frame_cropping_flag
            crop_left = reader.ue()
            crop_right = reader.ue()
            crop_top = reader.ue()
            crop_bottom = reader.ue()

        width = width_mbs * 16 - (crop_left + crop_right) * 2
        height = (2 - frame_mbs_only) * height_map_units * 16 - (crop_top + crop_bottom) * 2
        if 16 <= width <= 8192 and 16 <= height <= 8192:
            return width, height
    except (ValueError, IndexError):
        return None
    return None


def find_sps_annexb(data: bytes) -> Optional[bytes]:
    """在 Annex-B 码流里找到第一个 SPS NAL。"""
    index = 0
    length = len(data)
    while index < length - 4:
        start = data.find(b"\x00\x00\x01", index)
        if start < 0:
            return None
        nal_start = start + 3
        if nal_start >= length:
            return None
        if (data[nal_start] & 0x1F) == 7 and (data[nal_start] & 0x80) == 0:
            nal_end = data.find(b"\x00\x00\x01", nal_start)
            nal = data[nal_start:nal_end if nal_end > 0 else min(length, nal_start + 512)]
            if nal.endswith(b"\x00"):
                nal = nal.rstrip(b"\x00")
            return nal
        index = nal_start
    return None


def ts_payloads(data: bytes, max_packets: int = 2000) -> bytes:
    """把 MPEG-TS 包里的负载拼起来（只取包含 PES 的视频包，够找 SPS 即可）。"""
    out = bytearray()
    offset = data.find(b"\x47")
    if offset < 0:
        return b""
    packets = 0
    while offset + 188 <= len(data) and packets < max_packets:
        packet = data[offset:offset + 188]
        if packet[0] != 0x47:
            # 丢失同步，尝试重新对齐
            shift = data.find(b"\x47", offset + 1)
            if shift < 0:
                break
            offset = shift
            continue
        adaptation = (packet[3] >> 4) & 0x3
        body = packet[4:]
        if adaptation in (2, 3):
            if not body:
                break
            length = body[0]
            body = body[1 + length:]
        if adaptation in (1, 3) and body:
            out.extend(body)
        offset += 188
        packets += 1
    return bytes(out)


def _sps_from_avcc(data: bytes) -> Optional[bytes]:
    """fMP4 的 avcC box：[配置版本..][numOfSPS][SPS 长度(2B)][SPS]"""
    index = data.find(b"avcC")
    if index < 0:
        return None
    cursor = index + 4 + 5  # 跳过 box 名与 5 字节配置头
    if cursor + 3 > len(data):
        return None
    count = data[cursor] & 0x1F
    cursor += 1
    if count < 1 or cursor + 2 > len(data):
        return None
    length = int.from_bytes(data[cursor:cursor + 2], "big")
    cursor += 2
    if length <= 0 or cursor + length > len(data):
        return None
    return data[cursor:cursor + length]


def detect_resolution(data: bytes) -> str:
    """返回 "1920x1080" 形式的分辨率；解析不出来返回空串。"""
    if not data:
        return ""

    candidates = []
    if data[:1] == b"\x47" or b"\x47" in data[:376]:
        payload = ts_payloads(data)
        if payload:
            candidates.append(payload)
    sps_mp4 = _sps_from_avcc(data)
    if sps_mp4:
        parsed = parse_sps(sps_mp4)
        if parsed:
            return "%dx%d" % parsed
    candidates.append(data)

    for candidate in candidates:
        nal = find_sps_annexb(candidate)
        if nal:
            parsed = parse_sps(nal)
            if parsed:
                return "%dx%d" % parsed
    return ""
