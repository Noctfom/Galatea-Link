# 在线消息拆分兼容回归，覆盖宽提示误读开局消息和有界辅助帧降级

import struct

import pytest

from core.gamestate import DuelState
from core.parser import OCGParser, OCGParseError
from tests.test_online_state import start_payload


# 构造高四字节以开局指令号开头的宽提示，复现旧版 offset=7 报错
def wide_hint_packet(player=0):
    return bytes([2]) + struct.pack("<BBQ", 5, player, 0x0000000400000010)


# 新布局没有本地幽灵字节，旧拆分的实际错误来自提示数据被误读为新指令
def test_wide_hint_reproduces_offset_seven_and_recovers(caplog):
    packet = wide_hint_packet()
    with pytest.raises(OCGParseError) as caught:
        OCGParser.robust_parse(packet, strict=True)
    assert caught.value.layout_context == {"offset": 7, "first_type": 2, "packet_bytes": 11,
        "previous_type": 2, "previous_payload_bytes": 6, "failed_type": 4, "remaining_bytes": 3}
    assert OCGParser.robust_parse(packet, strict=True, compatibility=True) == [(2, packet[1:])]
    assert "message_wide_hint" in caplog.text


# 普通合并帧优先保持既有布局，宽提示之后的交互也不能被整帧吞掉
@pytest.mark.parametrize("wide", [False, True])
def test_hint_and_following_gameplay_messages_keep_their_boundaries(wide):
    hint = wide_hint_packet() if wide else bytes([2]) + struct.pack("<BBI", 5, 0, 123)
    prompt = struct.pack("<BI", 0, 123)
    following = bytes([40, 0, 13]) + prompt
    assert OCGParser.robust_parse(hint + following, strict=True, compatibility=True) == [
        (2, hint[1:]), (40, b"\0"), (13, prompt)]


# 未映射的宽玩家提示只省略这一条元数据，后续合法菜单仍然进入流程
def test_wide_player_hint_skips_metadata_but_keeps_following_prompt(caplog):
    payload = struct.pack("<BBQ", 0, 6, 0x0000000400000010)
    prompt = struct.pack("<BI", 0, 123)
    result = OCGParser.robust_parse(bytes([165]) + payload + bytes([13]) + prompt,
        strict=True, compatibility=True)
    assert result == [(13, prompt)]
    assert "auxiliary_message_skipped" in caplog.text


# 单独的辅助提示无法识别时可以跳过，诊断不包含卡号或完整载荷
def test_unrecognized_auxiliary_frame_only_warns(caplog):
    packet = bytes([160]) + struct.pack("<IBI", 0x01000400, 6, 123) + b"\x04\x04\x04\x04"
    assert OCGParser.robust_parse(packet, strict=True, compatibility=True) == []
    assert "auxiliary_frame_skipped" in caplog.text
    assert "first_type" in caplog.text and "previous_type" in caplog.text
    assert "01000400" not in caplog.text


# 真正的交互截断保持错误，不能用辅助降级掩盖等待玩家响应的菜单
def test_truncated_prompt_still_reports_its_original_type():
    with pytest.raises(OCGParseError) as caught:
        OCGParser.robust_parse(bytes([13, 0]), strict=True, compatibility=True)
    assert caught.value.layout_context["first_type"] == 13
    assert caught.value.layout_context["previous_type"] is None


# 已有游戏消息之后的截断不归入纯提示帧，保留准确的前置类型和计算长度
def test_gameplay_prefix_is_not_discarded_as_an_auxiliary_frame():
    packet = bytes([2]) + struct.pack("<BBI", 5, 0, 123) + bytes([40, 0, 4])
    with pytest.raises(OCGParseError) as caught:
        OCGParser.robust_parse(packet, strict=True, compatibility=True)
    assert caught.value.layout_context["previous_type"] == 40
    assert caught.value.layout_context["previous_payload_bytes"] == 1


# 提示到达时不改写挂起动作，不能在异步模型推理期间清空合法菜单
@pytest.mark.parametrize("kind,payload", [(2, wide_hint_packet()[1:]),
    (160, struct.pack("<IBI", 0x01000400, 6, 0)), (165, struct.pack("<BBI", 0, 6, 0)),
    (163, bytes(3)), (164, bytes(3))])
def test_hint_does_not_clear_pending_actions(kind, payload):
    state = DuelState()
    state.update(4, start_payload())
    state.update(41, struct.pack("<H", 4))
    state.update(13, struct.pack("<BI", 0, 123))
    actions = list(state.current_valid_actions)
    state.update(kind, payload)
    assert state.current_valid_actions == actions
