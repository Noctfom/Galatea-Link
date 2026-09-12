# 游戏内聊天模块，负责 YGOPro 聊天编解码和有界历史记录

import copy
import struct
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


CTOS_CHAT = 0x16
STOC_CHAT = 0x19
YGO_CHAT_MAX_UTF16_UNITS = 255


@dataclass(frozen=True)
class GameChatMessage:
    sequence: int
    created_at: float
    direction: str
    role: str
    source: str
    text: str
    player_type: int | None = None
    request_id: int | None = None

    # 转换为适合事件和外部接口使用的独立字典
    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "created_at": self.created_at,
            "direction": self.direction,
            "role": self.role,
            "source": self.source,
            "text": self.text,
            "player_type": self.player_type,
            "request_id": self.request_id,
        }


class GameChatHistory:
    # 初始化游戏聊天的有界内存记录
    def __init__(self, max_saved_messages: int = 100, max_saved_chars: int = 16000):
        if max_saved_messages < 1 or max_saved_chars < 1:
            raise ValueError("聊天历史容量必须大于 0")
        self._messages: deque[GameChatMessage] = deque()
        self._max_saved_messages = max_saved_messages
        self._max_saved_chars = max_saved_chars
        self._saved_chars = 0
        self._sequence = 0

    # 追加聊天记录并淘汰超出容量的最旧内容
    def append(
        self,
        *,
        direction: str,
        role: str,
        source: str,
        text: str,
        player_type: int | None = None,
        request_id: int | None = None,
    ) -> GameChatMessage:
        normalized_text = str(text).strip()
        if not normalized_text:
            raise ValueError("聊天内容不能为空")
        if direction not in {"inbound", "outbound"}:
            raise ValueError("聊天方向不受支持")
        if role not in {"agent", "opponent", "observer", "system"}:
            raise ValueError("聊天角色不受支持")
        normalized_source = str(source).strip()
        if not normalized_source:
            raise ValueError("聊天来源不能为空")

        self._sequence += 1
        message = GameChatMessage(
            sequence=self._sequence,
            created_at=time.time(),
            direction=direction,
            role=role,
            source=normalized_source,
            text=normalized_text,
            player_type=player_type,
            request_id=request_id,
        )
        self._messages.append(message)
        self._saved_chars += len(normalized_text)
        self._trim()
        return message

    # 返回按时间排序且受消息数和字符数限制的历史副本
    def snapshot(
        self,
        *,
        max_messages: int | None = None,
        max_chars: int | None = None,
    ) -> list[dict[str, Any]]:
        message_limit = self._max_saved_messages if max_messages is None else max_messages
        char_limit = self._max_saved_chars if max_chars is None else max_chars
        if message_limit < 1 or char_limit < 1:
            return []

        selected: list[GameChatMessage] = []
        selected_chars = 0
        for message in reversed(self._messages):
            if len(selected) >= message_limit:
                break
            next_size = selected_chars + len(message.text)
            if next_size > char_limit:
                break
            selected.append(message)
            selected_chars = next_size
        selected.reverse()
        return [copy.deepcopy(message.to_dict()) for message in selected]

    # 清空当前聊天历史并保留递增序号
    def clear(self) -> None:
        self._messages.clear()
        self._saved_chars = 0

    # 返回当前保存的聊天条数
    def __len__(self) -> int:
        return len(self._messages)

    # 查找指定时间范围内内容相同的最近出站聊天
    def find_recent_outbound(
        self,
        text: str,
        *,
        max_age_seconds: float = 10.0,
    ) -> GameChatMessage | None:
        cutoff = time.time() - max_age_seconds
        for message in reversed(self._messages):
            if message.created_at < cutoff:
                break
            if message.direction == "outbound" and message.text == text:
                return message
        return None

    # 淘汰超出保存上限的最旧聊天
    def _trim(self) -> None:
        while self._messages and (
            len(self._messages) > self._max_saved_messages
            or self._saved_chars > self._max_saved_chars
        ):
            removed = self._messages.popleft()
            self._saved_chars -= len(removed.text)


# 规范化聊天文本并按 UTF-16 编码单元限制长度
def normalize_game_chat_text(
    text: str,
    *,
    max_utf16_units: int = YGO_CHAT_MAX_UTF16_UNITS,
    truncate: bool = False,
) -> str:
    if isinstance(text, bytes):
        raise TypeError("聊天内容必须是字符串")
    if not 1 <= max_utf16_units <= YGO_CHAT_MAX_UTF16_UNITS:
        raise ValueError("聊天 UTF-16 长度限制必须位于 1 到 255")
    normalized = " ".join(str(text).replace("\x00", "").split())
    if not normalized:
        raise ValueError("聊天内容不能为空")
    try:
        encoded = normalized.encode("utf-16le")
    except UnicodeEncodeError as error:
        raise ValueError("聊天内容包含无法编码的字符") from error
    if len(encoded) // 2 <= max_utf16_units:
        return normalized
    if not truncate:
        raise ValueError(f"聊天内容不能超过 {max_utf16_units} 个 UTF-16 编码单元")

    encoded = encoded[: max_utf16_units * 2]
    while encoded:
        try:
            return encoded.decode("utf-16le").strip()
        except UnicodeDecodeError:
            encoded = encoded[:-2]
    raise ValueError("聊天内容截断后为空")


# 构建 CTOS_CHAT 使用的 UTF-16LE 空结尾载荷
def encode_ctos_chat_payload(
    text: str,
    *,
    max_utf16_units: int = YGO_CHAT_MAX_UTF16_UNITS,
    truncate: bool = False,
) -> tuple[str, bytes]:
    normalized = normalize_game_chat_text(
        text,
        max_utf16_units=max_utf16_units,
        truncate=truncate,
    )
    return normalized, normalized.encode("utf-16le") + b"\x00\x00"


# 解析 STOC_CHAT 中的玩家类型和 UTF-16LE 空结尾文本
def decode_stoc_chat_payload(payload: bytes) -> tuple[int, str]:
    if not isinstance(payload, bytes):
        raise TypeError("聊天载荷必须是 bytes")
    if len(payload) < 4 or len(payload) % 2:
        raise ValueError("服务端聊天载荷长度无效")
    if len(payload) > 2 + (YGO_CHAT_MAX_UTF16_UNITS + 1) * 2:
        raise ValueError("服务端聊天载荷超过协议上限")

    player_type = struct.unpack_from("<H", payload, 0)[0]
    encoded_text = payload[2:]
    terminator = None
    for offset in range(0, len(encoded_text), 2):
        if encoded_text[offset : offset + 2] == b"\x00\x00":
            terminator = offset
            break
    if terminator is None:
        raise ValueError("服务端聊天载荷缺少空结尾")
    try:
        text = encoded_text[:terminator].decode("utf-16le")
    except UnicodeDecodeError as error:
        raise ValueError("服务端聊天文本不是有效 UTF-16LE") from error
    normalized = text.strip()
    if not normalized:
        raise ValueError("服务端聊天内容为空")
    return player_type, normalized


# 根据服务端玩家类型判断聊天角色
def classify_game_chat_role(player_type: int, ai_player_id: int | None) -> str:
    if ai_player_id in {0, 1, 2, 3} and player_type == ai_player_id:
        return "agent"
    if player_type in {0, 1, 2, 3}:
        return "opponent"
    if player_type == 7:
        return "observer"
    return "system"
