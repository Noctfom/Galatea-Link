# 在线到 V4 的映射回归，覆盖真实查询字段候选响应与只读 Core 对照

import copy
import json
import os
import struct
from dataclasses import replace

import pytest

from agents.ai_bot import AiBot
from core.gamestate import DuelState
from core.observation import LlmObservationBuilder
from game_constants import LocationInfo
from model_protocols.v4.data_types import ActionOperation, SummonMethod, CardRelationType
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.online_adapter import project_online_snapshot
from tests.v4_online_fixtures import FixtureCardReader, full_query, make_state, readonly_core_reference
from tests.v4_fixtures import write_v4_assets


# 保持查询属性测试不依赖本机 cards.cdb
@pytest.fixture(autouse=True)
def fixed_card_reader(monkeypatch):
    monkeypatch.setattr("core.gamestate.card_db", FixtureCardReader())


# 明确零等级与阶级连接值分开进入 V4，关系按完整索引建成
def test_complete_query_dynamic_fields_and_relations_map_to_v4():
    state, _ = make_state()
    projection = project_online_snapshot(state.get_snapshot(), 0, event_type=11)
    first, second = projection.snapshot.entities
    assert first.level == 0 and first.rank == 4 and first.link_rating == 2
    assert first.current_atk == 2500 and first.base_atk == 0
    assert first.status_mask == 9 and first.is_disabled and first.properly_summoned
    assert first.counter_items == [(5, 3)]
    assert first.is_equipped and second.is_equip_source and not second.is_equipped
    assert (int(CardRelationType.EQUIP_TARGET), 0) in set(zip(second.relation_types, second.relation_indices))
    assert (int(CardRelationType.EQUIP_SOURCE), 1) in set(zip(first.relation_types, first.relation_indices))
    assert projection.report["groups"]["cards"]["source_complete"] is True
    assert projection.report["groups"]["cards"]["excluded_extra_inventory"] == 1


# V4 Idle 补齐专用语义但候选次序及物理响应与 V3 保持一致
def test_actions_preserve_wire_responses_and_initial_deck_is_stable():
    state, _ = make_state()
    source = state.get_snapshot()
    projection = project_online_snapshot(source, 0, event_type=11)
    target = projection.snapshot
    assert target.valid_actions[0].operation_id == int(ActionOperation.NORMAL_SUMMON)
    assert target.valid_actions[0].summon_method_id == int(SummonMethod.NORMAL)
    assert target.valid_actions[1].operation_id == int(ActionOperation.SPECIAL_SUMMON)
    assert target.valid_actions[1].summon_method_id == int(SummonMethod.SPECIAL_UNKNOWN)
    assert target.valid_actions[1].target_entity_idx == -1
    bot = AiBot(initialize_network=False)
    assert [bot.pack_choice_from_snapshot(source, index, 11) for index in range(len(source.valid_actions))] == [
        bot.pack_choice_from_snapshot(target, index, 11) for index in range(len(target.valid_actions))]
    state.p0_deck.clear()
    assert target.p0_initial_deck_codes == [14558127, 89631139]
    assert source.valid_actions[0].operation_id == 15
    assert target.p1_initial_deck_codes == []


# 查询失效后不再把旧动态值作为本次模型事实，未受影响卡片保持有效
def test_movement_invalidates_old_query_without_erasing_other_cards():
    state, _ = make_state()
    old = LocationInfo.encode(0, 4, 0, 1)
    new = LocationInfo.encode(0, 16, 0, 1)
    state.update(50, struct.pack("<IIII", 14558127, old, new, 0))
    snapshot = state.get_snapshot()
    other = next(entity for entity in snapshot.entities if entity.location == 4)
    assert other.current_atk == 2500
    assert getattr(other, "equip_target_location", 0) == 0
    projection = project_online_snapshot(snapshot, 0)
    assert projection.report["groups"]["cards"]["missing_card_count"] > 0
    state.update(7, bytes([0, 16, 0]) + full_query(0, 16, 0, current_atk=1800))
    assert next(entity for entity in state.get_snapshot().entities if entity.location == 16).current_atk == 1800


# 仅改变表示时旧实时属性会失效而不重新解释成数据库默认值
def test_position_change_is_reported_as_invalidated_query():
    state, _ = make_state()
    state.update(53, struct.pack("<IBBBBB", 14558127, 0, 4, 0, 1, 4))
    card = next(item for item in LlmObservationBuilder().build(state.get_snapshot(), 0)["cards"] if item["sequence"] == 0 and item["location_id"] == 4)
    assert "current_atk" not in card and card["dynamic_state_source"] == "query_invalidated"


# 诊断快照不产生边界也不能通过修改报告开关进入正常推理
def test_projection_is_non_mutating_and_stays_outside_inference(tmp_path):
    state, _ = make_state()
    original = copy.deepcopy(state.current_valid_actions)
    projection = project_online_snapshot(state.get_snapshot(), 0, event_type=11)
    assert state.current_valid_actions == original
    assert projection.report["groups"]["transitions"]["source_complete"] is False
    assert "14558127" not in json.dumps(projection.report)
    projection.report["online_inference_ready"] = True
    projection.snapshot._link_v4_projection = False
    encoder = GalateaEncoder(write_v4_assets(tmp_path / "assets"))
    with pytest.raises(ValueError, match="诊断投影"):
        encoder.encode(projection.snapshot, 0)


# 独立执行 Core 原始 getter 和动作解析后逐字段比较，而非共用 Link 的转换器
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
@pytest.mark.parametrize("player", [0, 1])
def test_projection_matches_readonly_core_snapshot_and_encoder(tmp_path, player):
    state, rows = make_state(player)
    reference = readonly_core_reference(os.environ["GALATEA_CORE_READONLY_ROOT"], state, rows, player)
    projection = project_online_snapshot(state.get_snapshot(), player, event_type=11)
    encoder = GalateaEncoder(write_v4_assets(tmp_path / "assets"))
    report = encoder.audit_projection(projection, reference, player)
    assert report["all_fields_equal"], report["different_fields"]
    assert report["field_count"] == 108
    assert report["grants_inference_permission"] is False


# 本方响应区间不能替代双方逐决策历史，即使其余输入完全相同
def test_own_response_interval_does_not_claim_native_history_equivalence(tmp_path):
    from model_protocols.v4.data_types import GameAction as Action4, TransitionBoundary
    from model_protocols.v4.event_history import TransitionEventRecorder, TransitionStateDigest
    state, _ = make_state()
    digest = state.history_digest()
    own_action = type(state.current_valid_actions[0])(13, 0, operation_id=2)
    token = state.response_history.prepare(request_id=1, actor=0, prompt_type=13, turn=1,
        phase=4, action=own_action, choice_id=0, digest=digest)
    state.response_history.confirm(token)
    message = struct.pack("<II", 89631139, LocationInfo.encode(1, 4, 0, 1))
    state.response_history.observe(60, message, digest)
    state.response_history.observe(13, b"", digest, interaction=True)
    projection = project_online_snapshot(state.get_snapshot(), 0, event_type=11)
    native = TransitionEventRecorder()
    native_digest = TransitionStateDigest(digest.lp_p0, digest.lp_p1, digest.zone_counts, digest.chain_depth)
    native.begin(actor=0, turn_count=1, phase_id=4, prompt_type=13,
        action=Action4(13, 0, operation_id=2), start_digest=native_digest,
        source_visibility_mask=1, target_visibility_mask=1)
    native.mark_boundary(TransitionBoundary.NEXT_DECISION)
    native.finalize(native_digest)
    native.begin(actor=1, turn_count=1, phase_id=4, prompt_type=11,
        action=Action4(0, 0, operation_id=23, code=89631139), start_digest=native_digest,
        source_visibility_mask=3, target_visibility_mask=3)
    native.observe_message(60, message, 0)
    native.mark_boundary(TransitionBoundary.NEXT_DECISION)
    native.finalize(native_digest)
    reference = replace(projection.snapshot, transition_history=native.snapshot())
    report = GalateaEncoder(write_v4_assets(tmp_path / "assets")).audit_projection(projection, reference, 0)
    assert not report["all_fields_equal"]
    assert "event_mask" in report["different_fields"]
    assert all(name.startswith("event_") for name in report["different_fields"])
    assert report["source_readiness"]["online_inference_ready"] is False
