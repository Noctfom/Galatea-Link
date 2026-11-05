# 在线游戏消息拆分模块，严格校验消息边界并与模型协议独立
import io
import struct

from core.gamestate import AUXILIARY_MESSAGE_TYPES, INTERACTION_MIN_PAYLOAD_LENGTHS, MessageParser
from core.protocol_warnings import LOGGER
from core.confirmation_protocol import confirmation_payload_length
from core.chain_protocol import chain_payload_length
from core.reload_protocol import reload_payload_length


WIDE_AUXILIARY_PAYLOAD_LENGTHS = {2: 10, 165: 10}


class OCGParseError(ValueError):
    pass


class OCGParser:
    # 兼容已知宽提示并单独降级可选提示帧，不扫描载荷字节猜测下一条指令
    @staticmethod
    def robust_parse(data, *, strict=False, compatibility=False, on_warning=None, confirmation_header_size=None,
                     chain_entry_size=None):
        try:
            return OCGParser._split_messages(data, confirmation_header_size=confirmation_header_size,
                chain_entry_size=chain_entry_size)
        except OCGParseError as initial_error:
            error = initial_error
        if data and (data[0] == 31 or error.layout_context["failed_type"] == 31 or any(kind == 31 for kind, _ in error.partial_messages)):
            for header_size in (3, 2):
                try:
                    return OCGParser._split_messages(data, confirmation_header_size=header_size,
                        chain_entry_size=chain_entry_size)
                except OCGParseError:
                    pass
        if data and (data[0] == 16 or error.layout_context["failed_type"] == 16 or any(kind == 16 for kind, _ in error.partial_messages)):
            for entry_size in (14, 13):
                try:
                    return OCGParser._split_messages(data, confirmation_header_size=confirmation_header_size,
                        chain_entry_size=entry_size)
                except OCGParseError:
                    pass
        if compatibility and data:
            first_type = data[0]
            wide_length = WIDE_AUXILIARY_PAYLOAD_LENGTHS.get(first_type)
            if wide_length is not None and len(data) >= 1 + wide_length:
                try:
                    messages = OCGParser._split_messages(data, first_payload_length=wide_length,
                        confirmation_header_size=confirmation_header_size, chain_entry_size=chain_entry_size)
                except OCGParseError as variant_error:
                    error = variant_error
                else:
                    OCGParser._warn("message_wide_hint", on_warning, first_type=first_type,
                        packet_bytes=len(data), payload_bytes=wide_length)
                    if first_type != 2:
                        # 高位描述尚未映射到旧提示语义，仅跳过这一项并保留后续可解析消息
                        OCGParser._warn("auxiliary_message_skipped", on_warning,
                            first_type=first_type, packet_bytes=len(data), reason="wide_metadata")
                        messages = messages[1:]
                    return messages
            if first_type in AUXILIARY_MESSAGE_TYPES and all(
                kind in AUXILIARY_MESSAGE_TYPES for kind, _ in error.partial_messages
            ):
                OCGParser._warn("auxiliary_frame_skipped", on_warning, **error.layout_context)
                return []
            LOGGER.error("在线消息边界解析失败 %s", error)
        if strict:
            raise error
        print(error)
        return error.partial_messages

    # 将兼容警告送入当前对局统计或独立协议日志，不记录载荷内容
    @staticmethod
    def _warn(code, callback, **details):
        if callback is not None:
            callback(code, **details)
        else:
            LOGGER.warning("在线协议兼容警告 %s %s", code, details)

    # 根据声明的布局确定边界，失败时附带前置类型与长度用于排查错位
    @staticmethod
    def _split_messages(data, *, first_payload_length=None, confirmation_header_size=None, chain_entry_size=None):
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
                if start == 0 and first_payload_length is not None:
                    length = first_payload_length
                elif msg_type == 4:
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
                elif msg_type == 31:
                    length = confirmation_payload_length(data[stream.tell():], preferred_header_size=confirmation_header_size)
                elif msg_type == 16:
                    length = chain_payload_length(data[stream.tell():], preferred_entry_size=chain_entry_size)
                elif msg_type == 162:
                    length = reload_payload_length(data[stream.tell():])
                else:
                    length = MessageParser.MSG_LEN.get(msg_type, -1)
                    if length == -1:
                        length = MessageParser.calculate_dynamic_length(msg_type, stream)
                if length < 0 or length > data_len - stream.tell():
                    raise OCGParseError(f"Type {msg_type} 长度无效或载荷截断")
                payload = stream.read(length)
                msgs.append((msg_type, payload))
            except (ValueError, struct.error, IndexError) as error:
                context = {"offset": start, "first_type": data[0], "packet_bytes": data_len,
                    "previous_type": msgs[-1][0] if msgs else None,
                    "previous_payload_bytes": len(msgs[-1][1]) if msgs else None,
                    "failed_type": msg_type, "remaining_bytes": data_len - stream.tell()}
                failure = OCGParseError(f"[OCG 解析拒绝] {context} {error}")
                failure.layout_context = context
                failure.partial_messages = msgs
                raise failure from error
        return msgs
