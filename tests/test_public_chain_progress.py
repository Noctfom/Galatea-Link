# 公开连锁通知回归，验证结算阶段无效类型快照隔离与模型历史保持原义

import struct

import pytest

from core.gamestate import DuelState
from core.observation import LlmObservationBuilder
from core.parser import OCGParser
from game_constants import LocationInfo
from tests.test_card_relations import FakeCardReader


# 构造当前在线布局的公开发动消息，不包含任何额外核心查询
def chaining(player=0, number=1):
    return struct.pack("<IIBBBIB", 14558127, LocationInfo.encode(player, 4, 0, 1),
        player, 4, 0, 14558127 * 16, number)


# 双方座位均能区分发动无效和效果无效，模型连锁及历史字典不增加这些状态
@pytest.mark.parametrize("player", [0, 1])
def test_chain_progress_reaches_llm_without_rewriting_model_history(player):
    state = DuelState()
    reader = LlmObservationBuilder(FakeCardReader())
    state.update(70, chaining(player))
    original = state.get_snapshot()
    state.update(71, b"\1")
    state.update(76, b"\1")
    observation = reader.build(state.get_snapshot(), player)
    current = observation["chain"][0]
    assert current["controller"] == "self" and current["chain_number"] == 1
    assert current["stage"] == "confirmed"
    assert current["effect_disabled_observed"] and not current["activation_negated_observed"]
    state.update(72, b"\1")
    assert reader.build(state.get_snapshot(), player)["chain"][0]["stage"] == "resolving"
    state.update(75, b"\1")
    state.update(73, b"\1")
    state.update(74, b"")
    final = state.get_snapshot()
    observation = reader.build(final, player, event_type=74)
    assert not observation["chain"]
    recent = observation["recent_history"][0]
    assert recent["stage"] == "resolved" and recent["resolution_observed"]
    assert recent["activation_negated_observed"] and recent["effect_disabled_observed"]
    assert recent["chain_ended"]
    assert "stage" not in original.chain_stack[0]
    assert final.history_stack == original.history_stack
    assert original.public_chain_progress["current"][0]["stage"] == "announced"
    assert not original.public_chain_progress["current"][0]["chain_ended"]


# 连锁结束不推定未收到的结算成功，未知编号不能凭空生成卡片或历史
def test_missing_resolution_and_unknown_number_stay_explicit():
    state = DuelState()
    state.update(75, b"\7")
    assert not state.public_chain_progress.current
    state.update(70, chaining())
    state.update(71, b"\1")
    state.update(74, b"")
    observation = LlmObservationBuilder(FakeCardReader()).build(state.get_snapshot(), 0)
    recent = observation["recent_history"][0]
    assert recent["chain_ended"] and not recent["resolution_observed"]
    assert recent["stage"] == "confirmed"
    assert observation["information_quality"]["chain_notifications"]["unmatched_notifications"] == 1
    state.reset()
    assert not state.public_chain_progress.snapshot()["observed"]


# 已有一字节连锁通知及四字节解除装备不会破坏混合网络消息边界
def test_chain_and_unequip_messages_keep_existing_wire_boundaries():
    wire = b"\x46" + chaining() + b"\x47\1\x48\1\x4c\1\x49\1\x4a"
    raw = LocationInfo.encode(0, 8, 0, 1)
    wire += b"\x5f" + struct.pack("<I", raw) + b"\x28\0"
    messages = OCGParser.robust_parse(wire, strict=True)
    assert [kind for kind, _ in messages] == [70, 71, 72, 76, 73, 74, 95, 40]
    state = DuelState()
    for kind, payload in messages:
        state.update(kind, payload)
    assert state.turn == 1
