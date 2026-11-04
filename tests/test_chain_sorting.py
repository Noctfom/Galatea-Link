# 连锁排序在线回归，覆盖消息边界双方座位完整响应菜单语义与只读模型对照

from dataclasses import replace
import itertools
import os
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from action_candidates import build_macro_action_pool
from agents.ai_bot import AiBot
from agents.decision_policy import DecisionOutcome
from app_config import DecisionConfig
from core.gamestate import MessageParser, get_interaction_player_id
from core.observation import LlmObservationBuilder
from core.parser import OCGParser, OCGParseError
from core.response_history import freeze_action
from core.rule_bot import get_rule_decision
from core.selection_options import build_selection_options
from core.selection_protocol import CANCEL_RESPONSE, SelectionProtocolError, parse_selection_prompt, validate_selection_response
from data_types import GameAction, ActionOperation
from galatea_link import GalateaLink
from model_protocols.v4.data_types import TransitionResultFlag
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.online_adapter import project_online_snapshot, project_own_history
from tests.test_complex_selection import CODES, Reader, state_with_cards
from tests.test_response_history import commit_link
from tests.test_v4_macro_projection import expected_options, readonly_macro_builder
from tests.v4_fixtures import write_v4_assets
from tests.v4_online_fixtures import full_query, readonly_core_reference


# 直接构造七字节候选，重复实体保留各自在提示中的索引
def sort_payload(player=0, indices=(0, 1, 2)):
    return bytes([player, len(indices)]) + b"".join(struct.pack("<IBBB", CODES[index], player, 4, index) for index in indices)


# 固定卡库元数据，测试不会读取或修改 Core 的运行资产
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    monkeypatch.setattr("core.gamestate.card_db", Reader())


# 将真实提示替换为完整可提交候选，保留基础候选供原生对照使用
def prepare_sort(state, payload, *, capacity=120):
    state.update(21, payload)
    base = list(state.current_valid_actions)
    state.current_valid_actions = build_macro_action_pool(21, payload, state, base, np.ones(len(base)),
        max_actions=capacity, rng=np.random.default_rng(42))
    return base


# 排序与后续提示混合时精确拆分，兼容旧入口且双方都读取实际行动者
@pytest.mark.parametrize("player", [0, 1])
def test_sort_chain_is_an_interaction_and_keeps_message_boundaries(player):
    payload = sort_payload(player)
    following = bytes([13, player]) + struct.pack("<I", 123)
    expected = [(21, payload), (13, following[1:])]
    assert OCGParser.robust_parse(bytes([21]) + payload + following, strict=True) == expected
    assert MessageParser.parse(bytes([21]) + payload + following) == [bytes([21]) + payload, following]
    assert get_interaction_player_id(21, payload) == player
    state, _ = state_with_cards(player)
    state.update(21, payload)
    assert state.active_player == player
    assert [action.index for action in state.current_valid_actions] == [0, 1, 2]


# 不允许本地幽灵字节或截断载荷混入七字节排序合同
@pytest.mark.parametrize("payload", [b"", b"\0", b"\0\3", sort_payload()[:-1]])
def test_sort_chain_truncation_is_rejected(payload):
    with pytest.raises(OCGParseError):
        OCGParser.robust_parse(bytes([21]) + payload, strict=True)


# 只有完整排列及明确默认顺序可以提交，数量前缀与缓冲区填充均无效
@pytest.mark.parametrize("bad", [b"\0\0\2", b"\0\1\3", b"\0\1", b"\3\0\1\2", b"\xff\0\0\0", b"\1\2\0" + bytes(509)])
def test_sort_chain_invalid_responses_are_rejected(bad):
    with pytest.raises(SelectionProtocolError):
        validate_selection_response(21, sort_payload(), bad)


# 默认顺序与自定义三循环同时可用，响应和菜单的候选身份必须一致
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("indices", [(0, 1, 2), (0, 0, 1)])
def test_chain_order_menu_matches_wire_and_preserves_prompt_indices(player, indices):
    state, _ = state_with_cards(player)
    payload = sort_payload(player, indices)
    prepare_sort(state, payload)
    snapshot = state.get_snapshot()
    projection = project_online_snapshot(snapshot, player, event_type=21)
    bot = AiBot(initialize_network=False)
    expected = {CANCEL_RESPONSE, *(bytes(order) for order in itertools.permutations(range(3)))}
    actual = {bot.pack_choice_from_snapshot(snapshot, index, 21) for index in range(7)}
    assert actual == expected
    assert validate_selection_response(21, payload, -1) == CANCEL_RESPONSE
    assert validate_selection_response(21, payload, b"\xff") == b"\xff"
    assert [action.sort_prompt_indices for action in projection.snapshot.valid_actions] == [action.sort_prompt_indices for action in snapshot.valid_actions]
    assert {bot.pack_choice_from_snapshot(projection.snapshot, index, 21) for index in range(7)} == expected
    menu = LlmObservationBuilder(Reader()).build(snapshot, player, event_type=21)
    assert menu["event"]["name"] == "sort_chain"
    context = menu["selection_context"]
    assert context["sort_kind"] == "chain" and context["default_order_available"] is True
    assert context["effect_descriptions_available"] is False
    for index, action in enumerate(snapshot.valid_actions):
        item = menu["legal_actions"][index]
        raw = bot.pack_choice_from_snapshot(snapshot, index, 21)
        assert item["action_name"] == "sort_chain"
        if raw == CANCEL_RESPONSE:
            assert item["sort_operation"] == "default" and "selection_items" not in item
            assert action.operation_id == int(ActionOperation.MACRO_SORT)
            assert action.cancelable is False
            continue
        order = sorted(range(3), key=lambda original: raw[original])
        assert item["sort_operation"] == "custom"
        assert [row["prompt_index"] for row in item["selection_items"]] == order
        assert [row["order"] for row in item["selection_items"]] == [1, 2, 3]
        assert action.macro_target_codes == [CODES[indices[original]] for original in order]
    assert projection.report["groups"]["actions"]["candidate_stage"] == "explicit_responses"
    assert projection.report["groups"]["actions"]["selection_response_checks"] is True
    assert not projection.report["online_inference_ready"]


# 非法行动者和多出的位置字节在解析阶段拒绝，不能当成另一种排序布局
@pytest.mark.parametrize("payload", [b"\2" + sort_payload()[1:], sort_payload() + b"\0"])
def test_sort_chain_rejects_unknown_layout_or_player(payload):
    with pytest.raises(SelectionProtocolError):
        parse_selection_prompt(21, payload)


# 单项及空排序保持字节响应，生成和缩减上限均固定保留默认顺序出口
@pytest.mark.parametrize("count", [0, 1, 8])
def test_chain_order_empty_single_and_bounded_large_pool(count):
    payload = sort_payload(indices=(0,) * count)
    options = build_selection_options(21, payload, limit=11)
    assert options[0]["bytes"] == CANCEL_RESPONSE and len(options) <= 11
    assert build_selection_options(21, payload, limit=1)[0]["bytes"] == CANCEL_RESPONSE
    state, _ = state_with_cards(0)
    prepare_sort(state, payload, capacity=1)
    assert len(state.current_valid_actions) == 1
    assert state.current_valid_actions[0].decision_bytes == CANCEL_RESPONSE
    for option in options:
        validate_selection_response(21, payload, option["bytes"])
    if count == 0:
        assert [option["bytes"] for option in options] == [CANCEL_RESPONSE, b""]


# 排序基础评分候选不能被误编码为单个四字节整数
@pytest.mark.parametrize("kind", [21, 25])
def test_sort_base_candidate_cannot_be_sent_as_integer(kind):
    with pytest.raises(ValueError, match="完整名次向量"):
        AiBot(initialize_network=False)._pack_response(GameAction(kind, 0), kind)


# 快照冻结提示索引且查询刷新保留菜单上下文，提示编号不被查询编号替换
def test_sort_indices_are_frozen_and_query_keeps_context():
    state, _ = state_with_cards(0)
    payload = sort_payload()
    prepare_sort(state, payload)
    source = next(action for action in state.current_valid_actions if action.sort_prompt_indices == [2, 0, 1])
    snapshot = state.get_snapshot()
    copied = next(action for action in snapshot.valid_actions if action.decision_bytes == source.decision_bytes)
    evidence = freeze_action(copied)
    source.sort_prompt_indices[0] = 99
    assert copied.sort_prompt_indices == [2, 0, 1]
    assert dict(evidence)["sort_prompt_indices"] == (2, 0, 1)
    source.sort_prompt_indices[0] = 2
    state.update(7, bytes([0, 4, 0]) + full_query())
    menu = LlmObservationBuilder(Reader()).build(state.get_snapshot(), 0, event_type=7)
    assert menu["selection_context"]["sort_kind"] == "chain"
    assert all(item["action_name"] == "sort_chain" for item in menu["legal_actions"])


# 规则回退不重复已拒绝的排序响应，也不能把默认顺序绕过硬禁用
def test_chain_sort_rule_fallback_respects_rejected_responses():
    payload = sort_payload()
    responses = [CANCEL_RESPONSE, *(bytes(order) for order in itertools.permutations(range(3)))]
    remaining = responses[-1]
    assert get_rule_decision(0, 21, bytes([21]) + payload, None, ignore_actions=responses[:-1]) == remaining
    with pytest.raises(SelectionProtocolError):
        get_rule_decision(0, 21, bytes([21]) + payload, None, ignore_actions=responses)


# 连锁排序基础候选与只读 Core 独立解析后比较全部一百零八项输入
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
@pytest.mark.parametrize("player", [0, 1])
def test_chain_sort_base_inputs_match_readonly_core(tmp_path, player):
    state, rows = state_with_cards(player)
    payload = sort_payload(player)
    state.update(21, payload)
    reference = readonly_core_reference(os.environ["GALATEA_CORE_READONLY_ROOT"], state, rows, player, msg_type=21, payload=payload)
    projection = project_online_snapshot(state.get_snapshot(), player, event_type=21)
    report = GalateaEncoder(write_v4_assets(tmp_path / "assets")).audit_projection(projection, reference, player)
    assert report["field_count"] == 108 and report["all_fields_equal"], report["different_fields"]


# 自定义排序按相同独立预期执行原生宏构建，默认顺序是 Link 的独立附加候选
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
@pytest.mark.parametrize("player", [0, 1])
def test_chain_sort_custom_macro_inputs_match_readonly_core(tmp_path, player):
    state, rows = state_with_cards(player)
    payload = sort_payload(player)
    options = expected_options(25, payload, {bytes(order) for order in itertools.permutations(range(3))})
    build_native = readonly_macro_builder(os.environ["GALATEA_CORE_READONLY_ROOT"], options)
    # 原生构建器读取同一合法排列，避免用 Link 生成器充当预期
    def prepare(brain, base):
        return build_native(21, payload, brain, base, np.ones(len(base)))
    reference = readonly_core_reference(os.environ["GALATEA_CORE_READONLY_ROOT"], state, rows, player,
        msg_type=21, payload=payload, prepare_actions=prepare)
    prepare_sort(state, payload)
    actions = [action for action in state.current_valid_actions if action.decision_bytes != CANCEL_RESPONSE]
    actions.sort(key=lambda action: action.decision_bytes)
    reference.valid_actions.sort(key=lambda action: action.decision_bytes)
    for index, (action, expected) in enumerate(zip(actions, reference.valid_actions)):
        action.index = expected.index = index
    state.current_valid_actions = actions
    projection = project_online_snapshot(state.get_snapshot(), player, event_type=21)
    report = GalateaEncoder(write_v4_assets(tmp_path / "assets")).audit_projection(projection, reference, player)
    assert report["field_count"] == 108 and report["all_fields_equal"], report["different_fields"]


# 在线网络入口收到排序后只发起一次正常异步决策，不再触发协议中止
@pytest.mark.asyncio
@pytest.mark.parametrize("player", [0, 1])
async def test_online_sort_chain_is_prepared_and_scheduled(player):
    state, _ = state_with_cards(player)
    link = GalateaLink.__new__(GalateaLink)
    link.gamestate, link.ai_player_id, link.ai_core_player_id = state, player, player
    link.ai = AiBot(initialize_network=False)
    link.decision_policy = SimpleNamespace(config=DecisionConfig(mode="llm_only"), should_run_core=lambda: False)
    link._capture_llm_observation, link._publish_event = Mock(), Mock()
    link._schedule_decision = AsyncMock()
    link._abort_protocol_failure = AsyncMock()
    await link.handle_server_msg(1, bytes([21]) + sort_payload(player))
    link._abort_protocol_failure.assert_not_awaited()
    link._schedule_decision.assert_awaited_once()
    assert link._schedule_decision.call_args.args[0] == 21
    snapshot = link._schedule_decision.call_args.args[2]
    assert len(snapshot.valid_actions) == 7
    assert link.last_prompt_type == 21


# 提交边界拒绝不完整排序，默认顺序真实发送后不在历史中标记为取消
@pytest.mark.asyncio
async def test_sort_commit_validates_wire_and_default_is_not_cancellation():
    link, request, _ = commit_link(AsyncMock(return_value=True))
    state, _ = state_with_cards(0)
    state.response_history.bind(0)
    payload = sort_payload()
    prepare_sort(state, payload)
    link.gamestate = state
    request = replace(request, msg_type=21, raw_msg=bytes([21]) + payload, snapshot=state.get_snapshot())
    with pytest.raises(SelectionProtocolError):
        await link._commit_decision(request, DecisionOutcome(response=b"\0\0\2", source="llm"))
    link.client.send_decision.assert_not_awaited()
    default = next(index for index, action in enumerate(request.snapshot.valid_actions) if action.decision_bytes == CANCEL_RESPONSE)
    await link._commit_decision(request, DecisionOutcome(response=CANCEL_RESPONSE, source="llm", choice_id=default))
    link.client.send_decision.assert_awaited_once_with(CANCEL_RESPONSE)
    state.update(13, struct.pack("<BI", 0, 123))
    events, skipped = project_own_history(state.response_history.snapshot(), 0)
    assert skipped == 0 and len(events) == 1
    assert events[0].operation_id == int(ActionOperation.MACRO_SORT)
    assert not events[0].result_flags & int(TransitionResultFlag.CANCELLED)
