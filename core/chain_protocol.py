# 在线连锁询问协议，共用十四字节逐候选强制标志及旧版十三字节布局解析

import struct


# 按候选数量和已识别布局计算连锁消息边界，不扫描卡号和描述中的字节
def chain_payload_length(payload, *, preferred_entry_size=None):
    raw = bytes(payload)
    if len(raw) < 11:
        raise ValueError("连锁询问消息头截断")
    count = raw[1]
    lengths = {size: 11 + count * size for size in (14, 13)}
    if preferred_entry_size in lengths:
        length = lengths[preferred_entry_size]
        if length > len(raw):
            raise ValueError("连锁询问候选列表截断")
        return length
    for size in (14, 13):
        if lengths[size] == len(raw):
            return lengths[size]
    if lengths[14] <= len(raw):
        # 旧版卡号低字节可能落在新布局的强制标志位置，优先使用可识别的标志布局
        if count and raw[2] in (0, 1) and any(raw[12 + index * 14] not in (0, 1) for index in range(count)):
            return lengths[13]
        return lengths[14]
    if lengths[13] <= len(raw):
        return lengths[13]
    raise ValueError("连锁询问候选列表截断")


# 解码完整连锁载荷，区分特殊候选数与逐候选强制标志并保留原始候选次序
def parse_online_chain(payload):
    raw = bytes(payload)
    if len(raw) < 11:
        raise ValueError("连锁询问消息头截断")
    player, count, header_flag, timing, opponent_timing = struct.unpack_from("<BBBII", raw)
    entry_size = next((size for size in (14, 13) if len(raw) == 11 + count * size), None)
    if entry_size is None:
        raise ValueError("连锁询问候选数量与载荷长度不符")
    candidates = []
    for index in range(count):
        offset = 11 + index * entry_size
        if entry_size == 14:
            mode, forced, code, location, desc = struct.unpack_from("<BBIII", raw, offset)
        else:
            mode, code, location, desc = struct.unpack_from("<BIII", raw, offset)
            forced = header_flag
        candidates.append({"effect_mode": mode, "forced": bool(forced), "code": code,
            "location": location, "desc": desc})
    return {"player": player, "special_count": header_flag if entry_size == 14 else 0,
        "hint_timing": timing, "opponent_hint_timing": opponent_timing,
        "forced": any(row["forced"] for row in candidates), "candidates": candidates,
        "entry_size": entry_size if count else None}
