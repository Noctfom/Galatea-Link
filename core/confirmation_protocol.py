# 在线卡片展示协议，独立识别带面板标志和旧版无标志的消息头

import struct


# 依据完整记录长度及已识别布局确定 Type31 边界，不把卡片字节当作指令搜索
def confirmation_payload_length(payload, *, preferred_header_size=None):
    raw = bytes(payload)
    if len(raw) < 2:
        raise ValueError("卡片确认消息头截断")
    lengths = {2: 2 + raw[1] * 7}
    if len(raw) >= 3:
        lengths[3] = 3 + raw[2] * 7
    if preferred_header_size in (2, 3):
        length = lengths.get(preferred_header_size)
        if length is None or length > len(raw):
            raise ValueError("卡片确认列表截断")
        return length
    if lengths.get(3) == len(raw):
        return lengths[3]
    if 3 in lengths and raw[1] in (0, 1) and lengths[3] <= len(raw):
        return lengths[3]
    if lengths[2] <= len(raw):
        return lengths[2]
    raise ValueError("卡片确认列表截断")


# 解码已拆分的展示载荷，面板标志仅控制界面而不改变接收者获知的卡片
def parse_online_confirmation(kind, payload):
    raw = bytes(payload)
    if kind not in (30, 31, 42) or len(raw) < 2:
        raise ValueError("卡片展示载荷无效或截断")
    sizes = (3, 2) if kind == 31 else (2,)
    header_size = next((size for size in sizes if len(raw) >= size and len(raw) == size + 7 * raw[size - 1]), None)
    if header_size is None:
        raise ValueError("卡片展示数量与载荷长度不符")
    cards = [{"code": code, "controller": controller, "location": location, "sequence": sequence}
        for code, controller, location, sequence in struct.iter_unpack("<IBBB", raw[header_size:])]
    return {"player": raw[0], "skip_panel": bool(raw[1]) if header_size == 3 else None,
        "header_size": header_size, "cards": cards}
