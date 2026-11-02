# 在线游戏消息拆分模块，严格校验消息边界并与模型协议独立
import io
import struct

from core.gamestate import INTERACTION_MESSAGE_TYPES, INTERACTION_MIN_PAYLOAD_LENGTHS, MessageParser


class OCGParseError(ValueError):
    pass


class OCGParser:
    # 按当前兼容的在线布局拆分消息且不猜测 Core 本地缓冲区布局
    @staticmethod
    def robust_parse(data, *, strict=False):
        msgs = []
        stream = io.BytesIO(data)
        data_len = len(data)
        while stream.tell() < data_len:
            start = stream.tell()
            msg_type = stream.read(1)[0]
            try:
                if msg_type == 0 or msg_type not in MessageParser.MSG_LEN and msg_type not in (6, 7, 8):
                    raise OCGParseError(f"未知在线 OCG 指令 Type {msg_type}，拒绝吞噬剩余字节")
                available = data_len - stream.tell()
                minimum = INTERACTION_MIN_PAYLOAD_LENGTHS.get(msg_type, 0)
                if available < minimum:
                    raise OCGParseError(f"Type {msg_type} 载荷截断 {available}/{minimum}")
                if msg_type == 4:
                    length = 18
                elif msg_type in (6, 8):
                    # 整区刷新没有外层长度字段，必须占用独立 STOC_GAME_MSG 载荷
                    length = available
                elif msg_type == 7:
                    if available < 7:
                        raise OCGParseError("单卡刷新查询头截断")
                    header = data[stream.tell():stream.tell() + 7]
                    row_size = struct.unpack_from("<I", header, 3)[0]
                    if row_size != 4 and row_size < 8:
                        raise OCGParseError("单卡刷新查询行长度无效")
                    length = 3 + row_size
                else:
                    length = MessageParser.MSG_LEN.get(msg_type, -1)
                    if length == -1:
                        length = MessageParser.calculate_dynamic_length(msg_type, stream)
                if length < 0 or length > data_len - stream.tell():
                    raise OCGParseError(f"Type {msg_type} 长度无效或载荷截断")
                payload = stream.read(length)
                msgs.append((msg_type, payload))
            except (ValueError, struct.error, IndexError) as error:
                message = f"[OCG 解析拒绝] offset={start} {error}"
                if strict:
                    raise OCGParseError(message) from error
                print(message)
                break
        return msgs
