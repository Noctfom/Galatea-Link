# Link 独立提示状态回归，覆盖撤销实例生命周期隐私和在线入口

import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.gamestate import DuelState
from core.observation import LlmObservationBuilder
from core.public_context import CARD_QUESTION
from galatea_link import GalateaLink
from game_constants import LocationInfo, Zone
from tests.test_online_state import query_row, start_payload


# 建立带固定物理座位的在线状态
def started_state():
    state = DuelState()
    state.update(4, start_payload())
    return state


# 读取当前真正交给 LLM 的公开上下文
def observation(state, player=0):
    return LlmObservationBuilder().build(state.get_snapshot(), player)


# 构造卡片提示字节且不改变在线布局
def card_hint(raw, kind, value):
    return struct.pack("<IBI", raw, kind, value)


# 描述引用重复添加按次撤销且数值零不是清空命令
def test_card_hint_counts_and_zero_value_are_preserved():
    state = started_state()
    state.update(7, bytes([0, 4, 0]) + query_row())
    raw = LocationInfo.encode(0, 4, 0, 1)
    state.update(160, card_hint(raw, 6, 12345))
    state.update(160, card_hint(raw, 6, 12345))
    state.update(160, card_hint(raw, 5, 0))
    first = state.get_snapshot()
    hints = observation(state)["public_context"]["hints"]
    assert any(item["kind"] == "description" and item["count"] == 2 for item in hints)
    assert any(item["kind"] == "number" and item["value"] == 0 for item in hints)
    state.update(160, card_hint(raw, 7, 12345))
    state.update(160, card_hint(raw, 7, 12345))
    state.update(160, card_hint(raw, 0, 0))
    assert observation(state)["public_context"]["hints"] == []
    assert first.public_hints[1].count == 2


# 玩家提示可以限制墓地查看且撤销后恢复正常可见状态
def test_player_hint_grave_access_restriction_and_removal():
    state = started_state()
    state.update(7, bytes([0, 16, 0]) + query_row(zone=16))
    state.update(165, struct.pack("<BBI", 0, 6, CARD_QUESTION))
    blocked = observation(state)
    assert blocked["public_context"]["grave_access_restricted"] is True
    assert "code" not in blocked["cards"][0]
    assert all(item["code"] != 14558127 for item in blocked["card_catalog"])
    state.update(165, struct.pack("<BBI", 0, 7, CARD_QUESTION))
    assert observation(state)["cards"][0]["code"] == 14558127


# 不从对手隐藏宿主提示查询名称效果或间接卡密
def test_hidden_host_hint_does_not_leak_related_card():
    state = started_state()
    state.update(7, bytes([1, 4, 0]) + query_row(actor=1, position=8))
    state.update(160, card_hint(LocationInfo.encode(1, 4, 0, 8), 2, 89631139))
    visible = observation(state)
    assert visible["public_context"]["hints"] == []
    assert all(item["code"] != 89631139 for item in visible["card_catalog"])


# 实例移动保留说明引用并清除临时数值提示
def test_move_transfers_description_without_reusing_numeric_hint():
    state = started_state()
    state.update(7, bytes([0, 4, 0]) + query_row())
    old = LocationInfo.encode(0, 4, 0, 1)
    new = LocationInfo.encode(0, 4, 1, 1)
    state.update(160, card_hint(old, 6, 12345))
    state.update(160, card_hint(old, 5, 2))
    state.update(50, struct.pack("<IIII", 14558127, old, new, 0))
    hints = observation(state)["public_context"]["hints"]
    assert len(hints) == 1 and hints[0]["sequence"] == 1 and hints[0]["kind"] == "description"


# 刷新确认空槽后下一张占位卡不得继承旧提示
def test_empty_slot_refresh_removes_old_instance_hints():
    state = started_state()
    state.update(7, bytes([0, 4, 0]) + query_row())
    state.update(160, card_hint(LocationInfo.encode(0, 4, 0, 1), 6, 12345))
    state.update(7, bytes([0, 4, 0]) + struct.pack("<I", 4))
    state.update(7, bytes([0, 4, 0]) + query_row())
    assert observation(state)["public_context"]["hints"] == []


# 已公开顶卡被抽走后只保留合法手牌记忆而不继续冒充卡组位置
def test_public_deck_top_draw_and_shuffle_invalidation():
    state = started_state()
    state.update(38, struct.pack("<BBI", 1, 0, 89631139))
    positions = observation(state)["public_context"]["known_deck_positions"]
    assert positions[0]["player"] == "opponent" and positions[0]["sequence_from_bottom"] == 41
    state.update(90, struct.pack("<BBI", 1, 1, 0))
    visible = observation(state)
    assert visible["public_context"]["known_deck_positions"] == []
    assert visible["known_information"]["opponent_hand_unordered"][0]["code"] == 89631139
    assert "code" not in visible["cards"][0]
    state.update(38, struct.pack("<BBI", 1, 0, 14558127))
    state.update(32, bytes([1]))
    assert observation(state)["public_context"]["known_deck_positions"] == []


# 真实消息入口处理三类提示且不会额外请求一次后台决策
@pytest.mark.asyncio
async def test_public_messages_reach_observation_without_new_decision():
    state = started_state()
    state.update(7, bytes([0, 4, 0]) + query_row())
    link = GalateaLink.__new__(GalateaLink)
    link.gamestate = state
    link._prepare_prompt_snapshot = AsyncMock(side_effect=lambda *args: state.get_snapshot())
    link._capture_llm_observation = Mock()
    link._schedule_decision = AsyncMock()
    link._publish_event = Mock()
    raw = LocationInfo.encode(0, 4, 0, 1)
    packets = [(160, card_hint(raw, 6, 12345)), (165, struct.pack("<BBI", 1, 6, 12345)),
        (120, struct.pack("<II", raw, 14558127))]
    for kind, payload in packets:
        await link.handle_server_msg(1, bytes([kind]) + payload)
    snapshot = link._capture_llm_observation.call_args.args[1]
    context = LlmObservationBuilder().build(snapshot, 0)["public_context"]
    assert len(context["hints"]) == 2
    assert context["recent_notifications"][0]["name"] == "missed_effect"
    link._schedule_decision.assert_not_awaited()
    assert not link._publish_event.called


# 同一局的回合身份不与当前等待响应的玩家混用
def test_turn_context_and_reset_are_independent():
    state = started_state()
    state.update(40, bytes([1]))
    state.active_player = 0
    visible = observation(state)
    assert visible["turn"]["actor"] == "self"
    assert visible["turn"]["turn_player"] == "opponent"
    assert visible["turn"]["starting_player"] == "opponent"
    state.update(165, struct.pack("<BBI", 0, 6, 12345))
    state.reset()
    assert observation(state)["public_context"]["hints"] == []


# 区域重建不能冒充玩家级提示的撤销通知
def test_zone_rebuild_does_not_revoke_player_access_restriction():
    state = started_state()
    state.update(165, struct.pack("<BBI", 0, 6, CARD_QUESTION))
    state.update(35, bytes([0]))
    context = observation(state)["public_context"]
    assert context["grave_access_restricted"] is True
    assert context["tracked_hint_lifecycle_consistent"] is False


# 单方收到的合法私有信息不能通过切换观察者变成另一方的事实
def test_online_snapshot_remains_bound_to_actual_recipient():
    state = started_state()
    state.update(38, struct.pack("<BBI", 0, 0, 89631139))
    state.update(165, struct.pack("<BBI", 0, 6, 12345))
    snapshot = state.get_snapshot()
    assert snapshot.known_deck_cards[0].visibility == 1
    assert snapshot.public_hints[0].visibility == 1
    with pytest.raises(ValueError, match="实际接收消息"):
        LlmObservationBuilder().build(snapshot, 1)
