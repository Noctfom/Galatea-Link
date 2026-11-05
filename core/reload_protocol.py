# 在线场面快照协议，按七怪兽槽八魔陷槽读取公开占位数量与当前连锁

import io
import struct


# 读取完整字段并在边界不足时给出不包含卡片内容的结构错误
def _read_exact(stream, size, field):
    raw = stream.read(size)
    if len(raw) != size:
        raise ValueError(f"Type162 {field} 截断 {len(raw)}/{size}")
    return raw


# 按实际固定槽位消费快照，不根据规则编号猜测怪兽槽数量
def read_online_reload(stream):
    rule = _read_exact(stream, 1, "规则")[0]
    players, warnings = [], []
    for player in (0, 1):
        lp, = struct.unpack("<I", _read_exact(stream, 4, "基本分"))
        zones = []
        for zone, capacity, width in ((4, 7, 2), (8, 8, 1)):
            cards = []
            for sequence in range(capacity):
                occupied = _read_exact(stream, 1, "占位标志")[0]
                if occupied:
                    values = _read_exact(stream, width, "场上占位")
                    cards.append((sequence, values[0], values[1] if width == 2 else 0))
                    if occupied != 1:
                        warnings.append({"code": "reload_occupied_flag", "player": player,
                            "zone": zone, "index": sequence, "flag": occupied})
            zones.append(tuple(cards))
        counts = tuple(_read_exact(stream, 6, "区域数量"))
        if counts[5] > counts[4]:
            warnings.append({"code": "reload_extra_faceup_count", "player": player,
                "total": counts[4], "faceup": counts[5]})
        players.append({"lp": lp, "monsters": zones[0], "spells": zones[1], "counts": counts})
    chains = []
    count = _read_exact(stream, 1, "连锁数量")[0]
    for _ in range(count):
        chains.append(struct.unpack("<IIBBBI", _read_exact(stream, 15, "当前连锁")))
    return {"rule": rule, "players": tuple(players), "chains": tuple(chains), "warnings": tuple(warnings)}


# 计算快照占用长度，后续合并消息继续独立拆分
def reload_payload_length(payload):
    stream = io.BytesIO(bytes(payload))
    read_online_reload(stream)
    return stream.tell()


# 完整解码已拆分的场面快照，全部读取成功后才允许状态层应用
def parse_online_reload(payload):
    stream = io.BytesIO(bytes(payload))
    restored = read_online_reload(stream)
    if stream.read(1):
        raise ValueError("Type162 已拆分载荷存在未归属尾部")
    return restored
