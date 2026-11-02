# 在线刷新回归，覆盖边界校验已知零隐私隔离和不重复触发决策

import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import query_protocol as q
from core.gamestate import DuelState
from core.online_state import OnlineStateProjection
from core.observation import LlmObservationBuilder
from core.parser import OCGParser, OCGParseError
from data_types import GameAction
from galatea_link import GalateaLink


# 构造完整 BO1 Start 载荷
def start_payload(player=0):
    return struct.pack("<BBIIHHHH", player, 5, 7000, 8000, 40, 15, 42, 12)


# 构造有明确字段集合的单卡查询行
def query_row(actor=0, zone=4, index=0, position=1, *, flags=None, atk=0, level=0):
    flags = q.QUERY_CODE | q.QUERY_POSITION | q.QUERY_ATTACK | q.QUERY_LEVEL if flags is None else flags
    values = []
    for flag, value in ((q.QUERY_CODE, 14558127), (q.QUERY_POSITION, actor | zone << 8 | index << 16 | position << 24), (q.QUERY_LEVEL, level), (q.QUERY_ATTACK, atk)):
        if flags & flag:
            values.append(value & 0xFFFFFFFF)
    body = struct.pack("<I", flags) + b"".join(struct.pack("<I", value) for value in values)
    return struct.pack("<I", len(body) + 4) + body


# 验证完整 Start 被精确拆分且后续消息不会被吞掉
def test_start_framing_and_public_counts():
    messages = OCGParser.robust_parse(bytes([4]) + start_payload(1) + bytes([40, 0]), strict=True)
    assert messages == [(4, start_payload(1)), (40, b"\0")]
    state = DuelState()
    state.update(4, start_payload())
    snapshot = state.get_snapshot()
    assert snapshot.global_data.my_lp == 7000
    assert snapshot.global_data.op_deck_len == 42
    assert snapshot.global_data.op_extra_len == 12
    assert snapshot.online_coverage["online_v4_inference_ready"] is False


# 验证未知报文不再被伪装为成功解析的尾部消息
def test_strict_parser_rejects_unknown_and_truncated_packets():
    for data in (bytes([254, 40, 1]), bytes([0, 40, 1]), bytes([4, 0]), bytes([7, 0, 4])):
        with pytest.raises(OCGParseError):
            OCGParser.robust_parse(data, strict=True)


# 验证刷新中的已知零与未提供值不会混同
def test_query_zero_level_and_missing_defense_reach_llm():
    state = DuelState()
    state.update(4, start_payload())
    state.update(7, bytes([0, 4, 0]) + query_row(atk=0, level=0))
    snapshot = state.get_snapshot()
    entity = snapshot.entities[0]
    assert entity.level == 0
    observation = LlmObservationBuilder().build(snapshot, 0)
    card = observation["cards"][0]
    assert card["current_atk"] == 0 and card["level"] == 0
    assert "current_def" not in card
    assert card["dynamic_state_source"] == "server_query"


# 验证完整区域刷新删除旧槽位且不会制造新动作
def test_zone_refresh_replaces_occupancy_without_replacing_pending_actions():
    state = DuelState()
    state.update(4, start_payload())
    state.update(7, bytes([0, 4, 0]) + query_row())
    action = GameAction(action_type=13, index=1)
    state.current_valid_actions = [action]
    state.update(6, bytes([0, 4]) + struct.pack("<I", 4) * 7)
    assert not state.field_map[0][4]
    assert state.current_valid_actions == [action]


# 验证查询先验证整包再提交且失败不留下半个场面
def test_bad_zone_refresh_does_not_partially_mutate_state():
    view = OnlineStateProjection()
    view.start(start_payload())
    view.refresh(7, bytes([0, 4, 0]) + query_row(atk=100))
    previous = dict(view.cards)
    malformed = bytes([0, 4]) + query_row(atk=200) + struct.pack("<I", 99)
    with pytest.raises(ValueError):
        view.refresh(6, malformed)
    assert view.cards == previous


# 验证对手隐藏动态属性与主卡组查询身份不会进入投影
@pytest.mark.parametrize("actor,zone,position", [(1, 2, 1), (1, 4, 8), (0, 1, 1)])
def test_query_projection_masks_private_state(actor, zone, position):
    view = OnlineStateProjection()
    view.start(start_payload())
    view.refresh(7, bytes([actor, zone, 0]) + query_row(actor, zone, position=position, atk=3000))
    card = view.cards[(actor, zone, 0)]
    assert "code" not in card and "current_atk" not in card


# 验证显式缓存合同与默认保守处理有不同且可见的语义
def test_query_cache_modes_are_explicit():
    for mode in (None, "replace", "merge"):
        view = OnlineStateProjection(query_mode=mode)
        view.start(start_payload())
        view.refresh(7, bytes([0, 4, 0]) + query_row(atk=2500))
        view.refresh(7, bytes([0, 4, 0]) + query_row(flags=q.QUERY_ATTACK, atk=0))
        assert ("code" in view.cards[(0, 4, 0)]) == (mode == "merge")
        assert view.coverage()["query_cache_contract_confirmed"] == (mode is not None)


# 验证网络刷新不会为已有提示重新提交一次后台决策
@pytest.mark.parametrize("kind", [6, 7])
@pytest.mark.asyncio
async def test_query_packet_never_reschedules_pending_decision(kind):
    state = DuelState()
    state.update(4, start_payload())
    state.current_valid_actions = [GameAction(action_type=13, index=1)]
    link = GalateaLink.__new__(GalateaLink)
    link.gamestate = state
    link.ai_player_id = 0
    link._prepare_prompt_snapshot = AsyncMock(return_value=state.get_snapshot())
    link._capture_llm_observation = Mock()
    link._schedule_decision = AsyncMock()
    link._publish_event = Mock()
    payload = bytes([0, 4]) + query_row() + struct.pack("<I", 4) * 6 if kind == 6 else bytes([0, 4, 0]) + query_row()
    assert OCGParser.robust_parse(bytes([kind]) + payload, strict=True) == [(kind, payload)]
    await link.handle_server_msg(1, bytes([kind]) + payload)
    assert state.field_map[0][4][0]["code"] == 14558127
    link._schedule_decision.assert_not_awaited()
    assert not link._publish_event.called


# 拒绝缺失开局基线的刷新且不产生推测座位
def test_query_before_start_is_rejected():
    view = OnlineStateProjection()
    with pytest.raises(ValueError, match="Start"):
        view.refresh(7, bytes([0, 4, 0]) + query_row())
    assert view.cards == {} and view.perspective is None


# 解析失败必须取消旧决策并关闭连接而非留下无限等待
@pytest.mark.asyncio
async def test_protocol_failure_cancels_decision_and_records_end_reason():
    link = GalateaLink.__new__(GalateaLink)
    link.gamestate = DuelState()
    link.gamestate.current_valid_actions = [GameAction(action_type=13, index=0)]
    link.decision_coordinator = SimpleNamespace(cancel_active=AsyncMock())
    link.client = SimpleNamespace(close=AsyncMock())
    link.duel_active = True
    link._finish_duel = AsyncMock()
    link._publish_event = Mock()
    await link.handle_server_msg(1, bytes([254]))
    assert link.gamestate.current_valid_actions == []
    link.decision_coordinator.cancel_active.assert_awaited_once()
    link._finish_duel.assert_awaited_once_with("protocol_error")
    link.client.close.assert_awaited_once()


# 未验收的 V4 在线模式在联网前拒绝启动并执行资源清理
@pytest.mark.asyncio
async def test_v4_online_gate_rejects_before_connect():
    link = GalateaLink.__new__(GalateaLink)
    link.ai = SimpleNamespace(model_metadata={"online_inference_ready": False, "online_block_reason": "V4 观测合同待验收"})
    link.decision_policy = SimpleNamespace(config=SimpleNamespace(mode="hybrid"))
    link.client = SimpleNamespace(connect=AsyncMock())
    link.close = AsyncMock()
    with pytest.raises(RuntimeError, match="V4 观测合同"):
        await link.start()
    link.client.connect.assert_not_awaited()
    link.close.assert_awaited_once()
