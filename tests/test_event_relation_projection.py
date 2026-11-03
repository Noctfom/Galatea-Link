# 关系事件到 LLM 和 V4 的完整传导回归，覆盖解除重绑与查询缓存复活问题

import struct
import json

import pytest

from core import query_protocol as q
from core.gamestate import DuelState
from core.observation import LlmObservationBuilder
from game_constants import LocationInfo
from model_protocols.v4.online_adapter import project_online_snapshot
from tests.test_card_relations import FakeCardReader, make_card_info
from tests.v4_online_fixtures import make_state


# 构造双方公开场面，关系事件本身不要求先有卡片查询
def public_state(player):
    state = DuelState()
    state.update(4, struct.pack("<BBIIHHHH", player, 5, 8000, 8000, 40, 15, 40, 15))
    state.field_map[player][4][0] = make_card_info(10001, player)
    state.field_map[player][8][0] = make_card_info(10002, player)
    state.field_map[1 - player][4][0] = make_card_info(10003, 1 - player)
    return state


# 无查询场面中的装备事件也进入 V4，重绑和解除清掉旧目标的反向关系
@pytest.mark.parametrize("player", [0, 1])
def test_event_equips_retarget_and_unequip_reach_both_observations(player):
    state = public_state(player)
    source = LocationInfo.encode(player, 8, 0, 1)
    first = LocationInfo.encode(player, 4, 0, 1)
    second = LocationInfo.encode(1 - player, 4, 0, 1)
    state.update(93, struct.pack("<II", source, first))
    state.update(93, struct.pack("<II", source, second))
    projected = project_online_snapshot(state.get_snapshot(), player).snapshot
    cards = {card.code: card for card in projected.entities}
    assert not cards[10001].is_equipped and cards[10003].is_equipped
    assert cards[10002].is_equip_source and cards[10002].equip_target_location == second
    state.update(95, struct.pack("<I", source))
    projected = project_online_snapshot(state.get_snapshot(), player).snapshot
    assert all(not card.is_equipped and not card.is_equip_source for card in projected.entities)
    observation = LlmObservationBuilder(FakeCardReader()).build(state.get_snapshot(), player, event_type=95)
    assert observation["event"]["name"] == "unequip"
    assert all(not card.get("equip_target_entity_id") and not card.get("equipped_by_entity_ids") for card in observation["cards"])


# 取消取对象按物理位置匹配，已确认的增量查询不能复活旧的装备和目标
def test_event_removal_updates_merge_query_cache():
    state, _ = make_state()
    state.online_projection.query_mode = "merge"
    source = LocationInfo.encode(0, 4, 1, 1)
    target = LocationInfo.encode(0, 4, 0, 1)
    state.update(95, struct.pack("<I", source))
    state.update(97, struct.pack("<II", source, LocationInfo.encode(0, 4, 0, 4)))
    query = struct.pack("<III", 12, q.QUERY_POSITION, source)
    state.update(7, bytes([0, 4, 1]) + query)
    entity = next(card for card in state.get_snapshot().entities if card.location == 4 and card.sequence == 1)
    assert not entity.is_equip_source and not entity.equip_target_location
    assert not entity.target_locations and not entity.target_entity_indices
    assert not state.online_projection.cards[(0, 4, 1)]["equip_target"]
    assert state.online_projection.cards[(0, 4, 1)]["target_locations"] == []
    state.update(96, struct.pack("<II", source, target))
    projected = project_online_snapshot(state.get_snapshot(), 0).snapshot
    entity = next(card for card in projected.entities if card.location == 4 and card.sequence == 1)
    assert entity.target_locations == [target]
    state.update(97, struct.pack("<II", source, target))
    assert not next(card for card in project_online_snapshot(state.get_snapshot(), 0).snapshot.entities
        if card.location == 4 and card.sequence == 1).target_locations


# 已公开的目标位置不能使对手盖伏卡片身份进入观察或模型实体
@pytest.mark.parametrize("player", [0, 1])
def test_relation_does_not_reveal_hidden_target_identity(player):
    state = public_state(player)
    state.field_map[1 - player][4][0]["pos"] = 2
    source = LocationInfo.encode(player, 4, 0, 1)
    target = LocationInfo.encode(1 - player, 4, 0, 2)
    state.update(96, struct.pack("<II", source, target))
    observation = LlmObservationBuilder(FakeCardReader()).build(state.get_snapshot(), player)
    assert "10003" not in json.dumps(observation)
    hidden = next(card for card in project_online_snapshot(state.get_snapshot(), player).snapshot.entities
        if card.owner != player)
    assert hidden.code == 0
