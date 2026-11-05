# YGOPro 服务端查询夹具，独立固定传统请求掩码与实际序列化字段

import struct


# 构造固定七怪兽槽八魔陷槽的场面快照，不受报文规则编号影响
def reload_packet(*, rule=3, players=None, chains=None):
    records = players if players is not None else [
        {'lp': 6100, 'monsters': [(0, 1, 2), (6, 4, 0)], 'spells': [(7, 8, 0)], 'counts': (30, 5, 3, 2, 12, 1)},
        {'lp': 4200, 'monsters': [(2, 8, 0)], 'spells': [(0, 1, 0)], 'counts': (28, 4, 2, 1, 10, 0)}]
    links = [(14558127, 0x01000400, 0, 4, 0, 14558127 * 16)] if chains is None else chains
    payload = bytes([rule])
    for player in records:
        payload += struct.pack('<I', player['lp'])
        for name, capacity in (('monsters', 7), ('spells', 8)):
            cards = {entry[0]: entry[1:] for entry in player[name]}
            for sequence in range(capacity):
                if sequence in cards:
                    position, overlays = cards[sequence]
                    payload += bytes([1, position]) + (bytes([overlays]) if name == 'monsters' else b'')
                else:
                    payload += b'\0'
        payload += bytes(player['counts'])
    payload += bytes([len(links)]) + b''.join(struct.pack('<IIBBBI', *entry) for entry in links)
    return bytes([162]) + payload


# 构造每候选带强制标志的连锁消息，描述末字节二十二复现旧拆包的伪 Type22
def chain_packet(player=0, *, special_count=1, candidates=None, legacy=False, legacy_forced=0,
                 timing=0x01020304, opponent_timing=0x05060708):
    entries = [(0, 0, 14558127, player | 4 << 8 | 1 << 24, 0x16020304)] if candidates is None else candidates
    header = struct.pack("<BBBII", player, len(entries), legacy_forced if legacy else special_count,
        timing, opponent_timing)
    rows = b"".join(struct.pack("<BIII", mode, code, location, desc) if legacy
        else struct.pack("<BBIII", mode, forced, code, location, desc)
        for mode, forced, code, location, desc in entries)
    return bytes([16]) + header + rows


# 构造卡号首字节为二十九的确认消息，便于验证面板标志和旧版头部
def confirmation_packet(player=0, *, skip_panel=0, legacy=False, cards=None):
    entries = [(0x0102031D, 2, 0, 0)] if cards is None else cards
    header = bytes([player, len(entries)]) if legacy else bytes([player, skip_panel, len(entries)])
    return bytes([31]) + header + b"".join(struct.pack("<IBBB", *row) for row in entries)


# 构造八十字节仅卡号刷新，未声明尾部用于检查兼容跳过和消息边界
def padded_code_row(code=0, *, tail=None):
    extra = bytes(68) if tail is None else tail
    return struct.pack("<III", 12 + len(extra), 1, code) + extra


# 模拟 get_infos 对传统手牌场面及含连接字段查询的序列化，不为历史保留位写入数据
def server_query_row(actor=0, zone=2, index=0, *, flags=0x781FFF, code=14558127, position=1, attack=2500):
    values = ((0x1, code), (0x2, actor | zone << 8 | index << 16 | position << 24),
        (0x4, 0), (0x8, 33), (0x10, 4), (0x20, 0), (0x40, 16), (0x80, 16),
        (0x100, attack), (0x200, 1000), (0x400, 2500), (0x800, 1000), (0x1000, 1))
    body = struct.pack("<I", flags)
    body += b"".join(struct.pack("<I", value & 0xFFFFFFFF) for flag, value in values if flags & flag)
    if flags & 0x80000:
        body += struct.pack("<I", 8)
    if flags & 0x200000:
        body += struct.pack("<I", 0)
    if flags & 0x400000:
        body += struct.pack("<I", 0)
    if flags & 0x800000:
        body += struct.pack("<II", 0, 0)
    return struct.pack("<I", len(body) + 4) + body
