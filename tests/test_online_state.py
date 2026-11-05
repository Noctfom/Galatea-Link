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
from tests.online_server_fixtures import server_query_row


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


# 真实服务器隐藏查询行保留原长度和占位，不能保留已知身份或吞掉下一行
@pytest.mark.parametrize("mode", [None, "replace", "merge"])
def test_redacted_query_row_preserves_occupancy_and_clears_old_fields(mode):
    view = OnlineStateProjection(query_mode=mode)
    view.start(start_payload())
    view.refresh(7, bytes([0, 4, 0]) + query_row())
    redacted = struct.pack("<I", 80) + bytes(76)
    payload = bytes([0, 4]) + redacted + struct.pack("<I", 4) * 6
    view.refresh(6, payload)
    assert (0, 4, 0) in view.cards and view.counts[(0, 4)] == 1
    assert set(view.cards[(0, 4, 0)]) == {"query_sequence"}
    with pytest.raises(q.CoreQueryError):
        q.parse_legacy_card_query(redacted)
    view.refresh(7, bytes([0, 4, 0]) + redacted[:-1] + b"\x01")
    assert set(view.cards[(0, 4, 0)]) == {"query_sequence"}
    assert view.compatibility_warning_counts["query_extra_bytes"] == 1


# 服务端会回传请求掩码内无载荷的历史保留位，不能把本地查询字段集用作网络白名单
@pytest.mark.parametrize("flags,size", [(0x781FFF, 72), (0x181FFF, 64), (0xF81FFF, 80)])
@pytest.mark.parametrize("player", [0, 1])
def test_standard_server_query_masks_are_accepted(flags, size, player):
    state = DuelState(model_protocol_version=4)
    state.update(4, start_payload(player))
    row = server_query_row(player, flags=flags)
    assert len(row) == size
    rows = b"".join(server_query_row(player, index=index, flags=flags) for index in range(5))
    state.update(6, bytes([player, 2]) + rows)
    snapshot = state.get_snapshot()
    assert len(state.field_map[player][2]) == 5
    assert state.field_map[player][2][0]["code"] == 14558127
    assert state.field_map[player][2][0]["current_atk"] == 2500
    assert snapshot.online_coverage["started"]


# 历史位不携带公开性信息，不能靠该位揭示对手隐藏身份
def test_server_reserved_bit_never_reveals_hidden_cards():
    view = OnlineStateProjection(query_mode="merge")
    view.start(start_payload())
    view.refresh(7, bytes([1, 2, 0]) + server_query_row(1, code=89631139))
    assert set(view.cards[(1, 2, 0)]) == {"position", "query_sequence"}
    assert q.PUBLIC_CARD_QUERY_FLAGS & 0x100000 == 0


# 行边界完整时未知字段和附加数据只警告，单行字段截断降级未知占位
@pytest.mark.parametrize("failure", ["unsupported_flag", "extra_payload", "truncated_payload"])
def test_server_query_variations_warn_without_ending_the_duel(failure, caplog):
    view = OnlineStateProjection()
    view.start(start_payload())
    row = bytearray(server_query_row())
    if failure == "unsupported_flag":
        struct.pack_into("<I", row, 4, 0x1781FFF)
    elif failure == "extra_payload":
        row += struct.pack("<I", 1)
        struct.pack_into("<I", row, 0, len(row))
    else:
        del row[-4:]
        struct.pack_into("<I", row, 0, len(row))
    view.refresh(7, bytes([0, 2, 0]) + row)
    warning = {"unsupported_flag": "query_unknown_flags", "extra_payload": "query_extra_bytes", "truncated_payload": "query_row_unparsed"}[failure]
    assert view.compatibility_warning_counts[warning] == 1
    assert "query_flags" in caplog.text and "row_bytes" in caplog.text and "row_index" in caplog.text
    assert "14558127" not in caplog.text
    assert (0, 2, 0) in view.cards
    if failure == "truncated_payload":
        assert set(view.cards[(0, 2, 0)]) == {"query_sequence"}


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


# 缺失开局基线时忽略辅助刷新并记录警告，不产生推测座位
def test_query_before_start_is_ignored():
    view = OnlineStateProjection()
    assert view.refresh(7, bytes([0, 4, 0]) + query_row()) is None
    assert view.compatibility_warning_counts["query_before_start"] == 1
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
