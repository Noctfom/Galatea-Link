# 在线场面重建回归，覆盖固定槽位原子更新可见信息失效及异步旧决策取消

import ast
import asyncio
from copy import deepcopy
import io
import os
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.decision_runtime import DecisionCoordinator
from core.gamestate import DuelState, MessageParser
from core.observation import LlmObservationBuilder
from core.parser import OCGParseError, OCGParser
from core.public_context import CARD_QUESTION
from core.reload_protocol import parse_online_reload
from galatea_link import GalateaLink
from tests.online_server_fixtures import reload_packet
from tests.test_complex_selection import Reader
from tests.test_online_state import query_row, start_payload
from tests.test_public_chain_progress import chaining


# 使用固定静态卡片资料验证查询缺失不会变成虚构动态属性
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    monkeypatch.setattr('core.gamestate.card_db', Reader())


# 所有规则号均读取七怪兽槽八魔陷槽，后续提示保留完整消息边界
@pytest.mark.parametrize('rule', [0, 1, 3, 5])
def test_reload_fixed_slots_and_following_prompt(rule):
    packet = reload_packet(rule=rule)
    following = struct.pack('<BI', 0, 123)
    assert OCGParser.robust_parse(packet + bytes([13]) + following, strict=True) == [
        (162, packet[1:]), (13, following)]
    stream = io.BytesIO(packet[1:] + bytes([40, 0]))
    assert MessageParser.calculate_dynamic_length(162, stream) == len(packet) - 1
    assert stream.tell() == 0
    restored = parse_online_reload(packet[1:])
    assert restored['players'][0]['monsters'][-1] == (6, 4, 0)
    assert restored['players'][0]['spells'] == ((7, 8, 0),)


# 重建恢复资源与占位，清理旧身份关系和菜单，同时保留真正稳定的本方初始构筑
@pytest.mark.parametrize('player', [0, 1])
def test_reload_replaces_stale_state_without_fabricating_card_identities(player):
    state = DuelState(model_protocol_version=4)
    state.effect_slot_resolver = lambda *_: 2
    state.update(4, start_payload(player))
    state.set_own_deck(player, [14558127] * 40, [89631139] * 15)
    state.update(7, bytes([0, 4, 0]) + query_row(atk=9999))
    state.update(70, chaining())
    state.update(13, struct.pack('<BI', player, 123))
    state.update(165, struct.pack('<BBI', player, 6, CARD_QUESTION))
    state.known_hand_codes[1] = [89631139]
    state.recently_confirmed = [89631139]
    before = state.get_snapshot()
    state.update(162, reload_packet()[1:])
    snapshot = state.get_snapshot()
    assert (snapshot.global_data.my_lp, snapshot.global_data.op_lp) == (6100, 4200)
    assert (snapshot.global_data.my_deck_len, snapshot.global_data.op_deck_len) == (30, 28)
    assert (snapshot.global_data.my_extra_len, snapshot.global_data.op_extra_len) == (12, 10)
    assert (snapshot.global_data.my_hand_len, snapshot.global_data.op_hand_len) == (5, 4)
    assert (snapshot.global_data.my_grave_len, snapshot.global_data.op_grave_len) == (3, 2)
    assert not snapshot.valid_actions and snapshot.action_prompt_type is None
    assert all(entity.code == 0 for entity in snapshot.entities)
    assert state.field_map[0][4][0]['overlays'] == [0, 0]
    assert not state.history_stack and state.known_hand_codes == {0: [], 1: []}
    assert not state.recently_confirmed
    assert len(getattr(snapshot, f'p{player}_initial_deck_codes')) == 40
    assert snapshot.initial_deck_known[player]
    assert not snapshot.public_state_flags[0] and not snapshot.public_state_flags[5]
    assert snapshot.public_state_flags[3 + player]
    assert snapshot.observed_history_coverage['gaps']['state_rebuild'] == 1
    observation = LlmObservationBuilder(Reader()).build(snapshot, player, event_type=162)
    assert observation['event']['name'] == 'reload_field'
    card = next(card for card in observation['cards'] if card.get('dynamic_state_source') == 'reload_field')
    assert 'current_atk' not in card and 'current_def' not in card
    assert observation['chain'][0]['stage'] == 'snapshot'
    assert observation['chain'][0]['code'] == 14558127
    assert not observation['recent_history']
    assert before.entities[0].code == 14558127
    assert state.online_projection.coverage()['field_rebuilds'] == 1
    assert observation['information_quality']['online_coverage']['last_field_rebuild']['extra_faceup_counts'] == [1, 0]
    state.update(71, b'\1')
    assert state.public_chain_progress.snapshot()['current'][0]['stage'] == 'confirmed'
    state.update(74, b'')
    assert not state.chain_stack and not state.public_chain_progress.current
    state.update(7, bytes([0, 4, 0]) + query_row(atk=0))
    assert state.field_map[0][4][0]['code'] == 14558127
    assert state.field_map[0][4][0]['current_atk'] == 0


# 任意位置的真实截断均不允许部分更新状态，也不将残缺快照当作辅助提示丢弃
def test_truncated_reload_is_atomic():
    state = DuelState()
    state.update(4, start_payload())
    state.update(7, bytes([0, 4, 0]) + query_row())
    state.update(13, struct.pack('<BI', 0, 123))
    field = deepcopy(state.field_map)
    actions = deepcopy(state.current_valid_actions)
    counts = dict(state.online_projection.counts)
    packet = reload_packet()
    for size in (1, 5, 13, len(packet) - 1):
        with pytest.raises(OCGParseError):
            OCGParser.robust_parse(packet[:size], strict=True, compatibility=True)
        with pytest.raises(ValueError):
            state.update(162, packet[1:size])
        assert state.field_map == field and state.current_valid_actions == actions
        assert state.online_projection.counts == counts
        assert state.online_projection.field_rebuilds == 0


# 可界定的标志与额外数量差异只警告，不阻断真实快照的其他可用信息
def test_reload_semantic_differences_warn_and_missing_start_does_not_claim_seat():
    packet = bytearray(reload_packet(chains=[]))
    packet[6] = 2
    restored = parse_online_reload(packet[1:])
    assert restored['warnings'][0]['code'] == 'reload_occupied_flag'
    state = DuelState()
    state.update(162, packet[1:])
    assert not state.online_projection.started and state.online_projection.perspective is None
    assert state.online_projection.compatibility_warning_counts['reload_before_start'] == 1
    state.update(4, start_payload())
    state.update(162, packet[1:])
    assert state.online_projection.compatibility_warning_counts['reload_occupied_flag'] == 1
    assert state.my_lp == 6100


# 重建取消真正运行中的异步任务，后续 Retry 不得重新提交已失效的旧菜单
@pytest.mark.asyncio
async def test_reload_cancels_pending_decision_without_scheduling_new_action():
    state = DuelState()
    state.update(4, start_payload())
    state.update(13, struct.pack('<BI', 0, 123))
    started, cancelled = asyncio.Event(), asyncio.Event()
    committer = AsyncMock()
    # 等待取消以模拟仍在处理旧场面的远程模型
    async def worker(request):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    coordinator = DecisionCoordinator(worker, committer)
    await coordinator.submit(13, b'old', state.get_snapshot())
    await started.wait()
    link = GalateaLink.__new__(GalateaLink)
    link.gamestate = state
    link.ai_player_id = 0
    link.decision_coordinator = coordinator
    link.last_prompt_type, link.last_prompt_msg = 13, b'old'
    link.last_prompt_actions = list(state.current_valid_actions)
    link.last_decision, link.last_decision_choice_id = 1, 0
    link._prepare_prompt_snapshot = AsyncMock(side_effect=lambda *_: state.get_snapshot())
    link._capture_llm_observation = Mock()
    link._schedule_decision = AsyncMock()
    link._publish_event = Mock()
    link.client = SimpleNamespace(close=AsyncMock())
    try:
        await link.handle_server_msg(1, reload_packet())
        assert cancelled.is_set() and coordinator.active_request_id is None
        assert link.last_prompt_type is None and link.last_prompt_actions is None
        await link.handle_server_msg(1, b'\1')
        committer.assert_not_awaited()
        link._schedule_decision.assert_not_awaited()
        link.client.close.assert_not_awaited()
        assert not link._publish_event.called
    finally:
        await coordinator.close()


# 只读提取 Core 的固定槽位读取器核对结果，不导入或修改 Core 仓库
def test_readonly_core_reload_parser_already_uses_fixed_slots():
    root = os.environ.get('GALATEA_CORE_READONLY_ROOT')
    if not root:
        pytest.skip('未提供只读 Core 参考目录')
    tree = ast.parse((Path(root) / 'core_message_protocol.py').read_text(encoding='utf-8'))
    names = {'CoreMessageProtocolError', '_read_exact', 'read_reload_field', 'parse_reload_field'}
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    namespace = {'io': io, 'struct': struct}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<readonly Core Type162>', 'exec'), namespace)
    payload = reload_packet(rule=3)[1:]
    actual = parse_online_reload(payload)
    assert {key: value for key, value in actual.items() if key != 'warnings'} == namespace['parse_reload_field'](payload)
