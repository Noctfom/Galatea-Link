# 选择表示区域和宣言的独立对照，区分模型语义候选编号与真实网络响应

import copy
import itertools
import os
import struct
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from action_candidates import build_macro_action_pool
from agents.ai_bot import AiBot
from core.network import CTOS_RESPONSE, YgoNetClient
from core.observation import LlmObservationBuilder
from core.parser import OCGParser
from core.response_codec import encode_int_response
from core.response_history import response_bytes
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.online_adapter import action_values, project_online_snapshot
from game_constants import LocationInfo
from data_types import GameAction
from tests.v4_fixtures import write_v4_assets
from tests.v4_online_fixtures import FixtureCardReader, make_state, readonly_core_reference


KINDS = (12, 13, 14, 15, 18, 19, 24, 140, 141, 143)
GRIDS = (0, 4, 16, 25, 31)
ANNOUNCED_NUMBERS = (0, 9001, 0xFFFFFFFF)


# 每组样本使用明确网络字节，位置字节和描述编号刻意取不同数值
def prompt_payload(kind, player, *, count=1):
    if kind == 12:
        return bytes([player]) + struct.pack("<IBBBBI", 14558127, player, 4, 0, 8, 0x01020304)
    if kind == 13:
        return bytes([player]) + struct.pack("<I", 0x01020304)
    if kind == 14:
        return bytes([player, 3]) + struct.pack("<III", 3, 300, 30000)
    if kind == 15:
        rows = b"".join(struct.pack("<II", 14558127, LocationInfo.encode(player, 4, index, 1)) for index in range(2))
        return bytes([player, 1, 1, 1, 2]) + rows
    if kind in (18, 24):
        mask = 0xFFFFFFFF ^ sum(1 << grid for grid in GRIDS)
        return bytes([player, count]) + struct.pack("<I", mask)
    if kind == 19:
        return bytes([player]) + struct.pack("<IB", 14558127, 15)
    if kind in (140, 141):
        return bytes([player, count]) + struct.pack("<I", 0x80000011)
    if kind == 143:
        return bytes([player, 3]) + struct.pack("<III", *ANNOUNCED_NUMBERS)
    raise ValueError("未定义的对照样本")


# 独立列出候选的响应，区域编号按接收者映射成实际物理座位
def expected_responses(kind, player):
    if kind in (12, 13):
        return [1, 0]
    if kind in (14, 143):
        return [0, 1, 2]
    if kind == 15:
        return [b"\1\0", b"\1\1", b"\xff" * 4]
    if kind in (18, 24):
        return [bytes([player if grid < 16 else 1 - player, 8 if grid & 8 else 4, grid & 7]) for grid in GRIDS]
    if kind == 19:
        return [1, 2, 4, 8]
    return [1, 16, 0x80000000]


# 固定查询静态资料，卡名与文本只用于检查输出菜单
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    reader = FixtureCardReader()
    reader.get_card_name = lambda code: f"Card {code}"
    reader.get_card_text = lambda code: "测试文本"
    monkeypatch.setattr("core.gamestate.card_db", reader)
    return reader


# 十类提示在双方座位均保持候选次序和响应，解析后还能继续拆分下一条消息
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("kind", KINDS)
def test_prompt_semantics_and_wire_responses_remain_aligned(kind, player, fixed_reader):
    state, _ = make_state(player)
    payload = prompt_payload(kind, player)
    messages = OCGParser.robust_parse(bytes([kind]) + payload + b"\x28\0", strict=True)
    assert messages == [(kind, payload), (40, b"\0")]
    state.update(kind, payload)
    source = state.get_snapshot()
    before = copy.deepcopy(source.valid_actions)
    target = project_online_snapshot(source, player, event_type=kind).snapshot
    bot = AiBot(initialize_network=False)
    bot.player_id = 1 - player
    expected = [response_bytes(value) for value in expected_responses(kind, player)]
    for snapshot in (source, target):
        assert [response_bytes(bot.pack_choice_from_snapshot(snapshot, index, kind))
            for index in range(len(snapshot.valid_actions))] == expected
    assert source.valid_actions == before
    observation = LlmObservationBuilder(fixed_reader).build(source, player, event_type=kind)
    assert [item["choice_id"] for item in observation["legal_actions"]] == list(range(len(expected)))
    if kind == 12:
        assert [action.desc_id for action in source.valid_actions] == [0x01020304] * 2
        assert all(action.target_entity_idx == 0 for action in target.valid_actions)
        state.effect_slot_resolver = lambda code, desc: 3 if desc == 0x01020304 else None
        state.update(kind, payload)
        assert all(action.effect_slot == 3 for action in state.current_valid_actions)
    if kind == 143:
        assert [action.response_value for action in target.valid_actions] == list(ANNOUNCED_NUMBERS)
        assert [action.index for action in target.valid_actions] == [0, 1, 2]
        assert [action["announced_number"] for action in observation["legal_actions"]] == list(ANNOUNCED_NUMBERS)
        assert all("description_id" not in action for action in observation["legal_actions"])
    if kind == 19:
        assert [action["position_id"] for action in observation["legal_actions"]] == [1, 2, 4, 8]
    if kind in (15, 18, 24, 140, 141):
        assert observation["selection_context"]["minimum"] == observation["selection_context"]["maximum"] == 1


# 原始 Core 解析与快照独立执行，已知 Type12 位置遗漏仅标记为上游问题
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("kind", KINDS)
def test_prompt_inputs_match_readonly_core_or_report_upstream_issue(tmp_path, kind, player):
    state, rows = make_state(player)
    payload = prompt_payload(kind, player)
    state.update(kind, payload)
    reference = readonly_core_reference(os.environ["GALATEA_CORE_READONLY_ROOT"], state, rows,
        player, msg_type=kind, payload=payload)
    if kind == 12:
        actual = [action.desc_id for action in reference.valid_actions]
        if actual == [struct.unpack("<I", payload[8:12])[0]] * 2:
            pytest.xfail("Core Type12 动作解析未跳过位置字节，Link 按十三字节网络载荷修正")
        assert actual == [0x01020304] * 2
    projection = project_online_snapshot(state.get_snapshot(), player, event_type=kind)
    report = GalateaEncoder(write_v4_assets(tmp_path / "assets")).audit_projection(projection, reference, player)
    assert report["field_count"] == 108
    assert report["all_fields_equal"], report["different_fields"]
    assert not report["grants_inference_permission"]


# 最终区域与种族属性宏候选必须满足数量，语义和字节不得被池序号替代
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("kind", [15, 18, 24, 140, 141])
def test_final_macro_candidates_obey_selection_count_and_responses(kind, player):
    state, _ = make_state(player)
    payload = prompt_payload(kind, player, count=2)
    state.update(kind, payload)
    base = list(state.current_valid_actions)
    actions = build_macro_action_pool(kind, payload, state, base, np.ones(len(base)))
    state.current_valid_actions = actions
    source = state.get_snapshot()
    projected = project_online_snapshot(source, player, event_type=kind)
    bot = AiBot(initialize_network=False)
    actual = {response_bytes(bot.pack_choice_from_snapshot(projected.snapshot, index, kind)) for index in range(len(actions))}
    if kind == 15:
        expected = set(expected_responses(kind, player))
        assert any(action.operation_id == 7 and action.cancelable for action in projected.snapshot.valid_actions)
    elif kind in (18, 24):
        singles = expected_responses(kind, player)
        expected = {b"".join(parts) for parts in itertools.combinations(singles, 2)}
    else:
        expected = {encode_int_response(left | right) for left, right in itertools.combinations([1, 16, 0x80000000], 2)}
    assert actual == expected
    if kind != 15:
        assert all(action.selection_min == action.selection_max == action.selection_count == 2
            for action in projected.snapshot.valid_actions)
        observation = LlmObservationBuilder(FixtureCardReader(), include_card_text=False)
        observation.card_reader.get_card_name = lambda code: f"Card {code}"
        menu = observation.build(source, player, event_type=kind)
        assert menu["selection_context"]["minimum"] == menu["selection_context"]["maximum"] == 2
    assert not projected.report["online_inference_ready"]


# 数字宣言的模型值表示数字本身，实际网络仍发送选项编号
def test_legacy_number_semantics_keep_announced_value():
    values = action_values(GameAction(143, 1, desc_id=9001), 143)
    assert values["response_value"] == 9001


# 三十二位掩码的发送和历史匹配一致，越界数值不能截断成其他动作
@pytest.mark.asyncio
async def test_unsigned_mask_send_uses_same_bytes_as_history():
    client = YgoNetClient("127.0.0.1", 7911, "", None)
    client.send_packet = AsyncMock(return_value=True)
    assert await client.send_decision(0x80000001)
    client.send_packet.assert_awaited_once_with(CTOS_RESPONSE, b"\1\0\0\x80")
    assert response_bytes(0xFFFFFFFF) == response_bytes(-1) == b"\xff" * 4
    assert response_bytes(0x80000000) == response_bytes(-(1 << 31))
    for value in (1 << 32, -(1 << 31) - 1):
        with pytest.raises(ValueError):
            await client.send_decision(value)
    assert client.send_packet.await_count == 1


# 宣言数值位掩码和区域序号不应被当作效果描述查询或混入错误文本
@pytest.mark.parametrize("kind", [18, 19, 24, 140, 141, 143])
def test_non_effect_prompt_values_are_not_resolved_as_effect_text(kind, fixed_reader):
    state, _ = make_state()
    state.update(kind, prompt_payload(kind, 0))
    fixed_reader.get_effect_description = Mock(return_value="不应出现的效果说明")
    observation = LlmObservationBuilder(fixed_reader).build(state.get_snapshot(), 0, event_type=kind)
    fixed_reader.get_effect_description.assert_not_called()
    assert all("effect_description" not in item for item in observation["legal_actions"])
