# 最终宏候选独立对照，使用原生构建器和响应预期核对全部模型输入及大池出口

import ast
from copy import deepcopy
import itertools
import os
from pathlib import Path
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from action_candidates import build_macro_action_pool
from agents.ai_bot import AiBot
from core.branch_retry import CoreBranchRetry, action_token
from core.observation import LlmObservationBuilder
from core.response_history import response_bytes
from core.selection_protocol import CANCEL_RESPONSE, validate_selection_response
from model_protocols.v4 import data_types as native_types
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.online_adapter import project_online_snapshot
from game_constants import LocationInfo
from tests.test_complex_selection import CASES, Reader, case_payload, readonly_validator, state_with_cards
from tests.test_v4_prompt_projection import expected_responses, prompt_payload
from tests.v4_fixtures import write_v4_assets
from tests.v4_online_fixtures import readonly_core_reference


# 从独立响应预期恢复展示元数据，不复用 Link 的候选生成器
def expected_options(kind, payload, responses):
    cards, mandatory = [], []
    if kind in (15, 20):
        for index in range(payload[4]):
            code, player, zone, sequence, extra = struct.unpack_from("<IBBBB", payload, 5 + index * 8)
            cards.append((code, LocationInfo.encode(player, zone, sequence, extra if kind == 15 else 0), extra if kind == 20 else 0))
    elif kind == 22:
        for index in range(payload[5]):
            code, player, zone, sequence, _ = struct.unpack_from("<IBBBH", payload, 6 + index * 9)
            cards.append((code, LocationInfo.encode(player, zone, sequence, 0), 0))
    elif kind == 23:
        count, offset = payload[8], 9
        for size in (count, payload[9 + count * 11]):
            rows = []
            for _ in range(size):
                code, player, zone, sequence, value = struct.unpack_from("<IBBBI", payload, offset)
                rows.append((code, LocationInfo.encode(player, zone, sequence, 0), value))
                offset += 11
            if not mandatory and offset == 9 + count * 11:
                mandatory = rows
                offset += 1
            else:
                cards = rows
    elif kind == 25:
        for index in range(payload[1]):
            code, player, zone, sequence = struct.unpack_from("<IBBB", payload, 2 + index * 7)
            cards.append((code, LocationInfo.encode(player, zone, sequence, 0), index + 1))
    options = []
    for raw in sorted(responses):
        row = {"bytes": raw}
        selected = []
        if raw == CANCEL_RESPONSE:
            pass
        elif kind in (15, 20):
            selected = [cards[index] for index in raw[1:]]
        elif kind == 23:
            selected = mandatory + [cards[index] for index in raw[1 + len(mandatory):]]
        elif kind == 22:
            counts = struct.unpack("<" + "H" * len(cards), raw)
            selected = [(code, location, count) for (code, location, _), count in zip(cards, counts) if count]
        elif kind == 25:
            selected = [(cards[index][0], cards[index][1], rank + 1)
                for rank, index in enumerate(sorted(range(len(cards)), key=lambda index: raw[index]))]
        elif kind in (18, 24):
            places = []
            for offset in range(0, len(raw), 3):
                player, zone, sequence = raw[offset:offset + 3]
                places.append((16 if player != payload[0] else 0) + (8 if zone == 8 else 0) + sequence)
            row["places"] = places
        else:
            value = int.from_bytes(raw, "little")
            bits = [index for index in range(32) if value & 1 << index]
            row = {"value": value, "indices": bits, "values": [index + 1 for index in bits]}
        if selected:
            row.update(codes=[item[0] for item in selected], locs=[item[1] for item in selected], values=[item[2] for item in selected])
        options.append(row)
    return options


# 只读编译 Core 原始宏构建方法，独立预期替代生成器以隔离模型语义合同
def readonly_macro_builder(root, options):
    native = readonly_validator(root).__globals__
    tree = ast.parse((Path(root) / "action_candidates.py").read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.Assign))]
    namespace = {**native, **vars(native_types), "np": np, "struct": struct, "MAX_ACTIONS": 120,
        "LocationInfo": LocationInfo, "rule_bot": SimpleNamespace(get_macro_options=lambda *args, **kwargs: deepcopy(options))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<readonly Core macro builder>", "exec"), namespace)
    return namespace["build_macro_action_pool"]


# 不依赖卡库文件和在线服务获得静态查询数据
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    monkeypatch.setattr("core.gamestate.card_db", Reader())


# 原生最终宏输入逐字段对照，双方座位均覆盖数值选择与区域宣言
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("name", [*CASES[:-1], "place", "disfield", "race", "attribute"])
def test_final_macro_inputs_match_readonly_core(tmp_path, name, player):
    state, rows = state_with_cards(player)
    if name in CASES:
        kind, payload, responses = case_payload(name, player)
    else:
        kind = {"place": 18, "disfield": 24, "race": 140, "attribute": 141}[name]
        payload = prompt_payload(kind, player, count=2)
        singles = expected_responses(kind, player)
        responses = {b"".join(pair) for pair in itertools.combinations(singles, 2)} if kind in (18, 24) else {
            response_bytes(left | right) for left, right in itertools.combinations(singles, 2)}
    state.update(kind, payload)
    options = expected_options(kind, payload, responses)
    build_native = readonly_macro_builder(os.environ["GALATEA_CORE_READONLY_ROOT"], options)
    # 排序仅用于对齐语义相同的候选，不把临时宏池序号当作物理响应
    def prepare(brain, base):
        return build_native(kind, payload, brain, base, np.ones(len(base)))
    reference = readonly_core_reference(os.environ["GALATEA_CORE_READONLY_ROOT"], state, rows,
        player, msg_type=kind, payload=payload, prepare_actions=prepare)
    actions = build_macro_action_pool(kind, payload, state, state.current_valid_actions, np.ones(len(state.current_valid_actions)))
    actions.sort(key=lambda action: response_bytes(action.decision_value) if action.decision_value is not None else action.decision_bytes)
    reference.valid_actions.sort(key=lambda action: response_bytes(action.decision_value) if action.decision_value is not None else action.decision_bytes)
    for index, (action, expected) in enumerate(zip(actions, reference.valid_actions)):
        action.index = expected.index = index
    state.current_valid_actions = actions
    projected = project_online_snapshot(state.get_snapshot(), player, event_type=kind)
    report = GalateaEncoder(write_v4_assets(tmp_path / "assets")).audit_projection(projected, reference, player)
    assert len(actions) == len(reference.valid_actions) == len(responses)
    assert report["all_fields_equal"], report["different_fields"]
    assert report["field_count"] == 108
    assert not report["grants_inference_permission"]


# 大候选池按容量缩减时始终保留取消，合法响应和菜单编号保持一致
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("capacity", [1, 120])
def test_large_pool_preserves_cancel_and_valid_responses(player, capacity):
    state, _ = state_with_cards(player)
    payload = bytes([player, 1, 3, 3, 14]) + b"".join(struct.pack("<IBBBB", 1, player, 16, index, 1) for index in range(14))
    state.update(15, payload)
    base = list(state.current_valid_actions)
    for seed in range(5):
        actions = build_macro_action_pool(15, payload, state, base, np.ones(len(base)), max_actions=capacity, rng=np.random.default_rng(seed))
        assert len(actions) == capacity and actions[0].decision_bytes == CANCEL_RESPONSE
        assert len({action.decision_bytes for action in actions}) == capacity
        for action in actions:
            validate_selection_response(15, payload, action.decision_bytes)
        state.current_valid_actions = actions
        projected = project_online_snapshot(state.get_snapshot(), player, event_type=15)
        bot = AiBot(initialize_network=False)
        assert [bot.pack_choice_from_snapshot(projected.snapshot, index, 15) for index in range(capacity)] == [action.decision_bytes for action in actions]
        menu = LlmObservationBuilder(Reader()).build(state.get_snapshot(), player, event_type=15)
        assert [action["choice_id"] for action in menu["legal_actions"]] == list(range(capacity))


# 空字节也是明确响应，重新编号不能让末端回退丢失同一选择的身份
def test_empty_macro_response_identity_survives_resampling():
    one = native_types.GameAction(25, 0, decision_bytes=b"")
    one.explicit_response = True
    two = deepcopy(one)
    two.index = 119
    assert action_token(one) == action_token(two)


# 大池使用相同候选顺序和随机种子时，两侧缩减结果保持一致且取消固定保留
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
@pytest.mark.parametrize("player", [0, 1])
def test_large_pool_sampling_matches_readonly_core(player):
    state, _ = state_with_cards(player)
    payload = bytes([player, 1, 3, 3, 14]) + b"".join(struct.pack("<IBBBB", 1, player, 16, index, 1) for index in range(14))
    responses = {CANCEL_RESPONSE, *(bytes([3, *indices]) for indices in itertools.combinations(range(14), 3))}
    options = expected_options(15, payload, responses)
    native = readonly_macro_builder(os.environ["GALATEA_CORE_READONLY_ROOT"], options)
    state.update(15, payload)
    base = list(state.current_valid_actions)
    for seed in range(5):
        actual = build_macro_action_pool(15, payload, state, base, np.ones(len(base)), rng=np.random.default_rng(seed))
        expected = native(15, payload, state, base, np.ones(len(base)), rng=np.random.default_rng(seed))
        assert [action.decision_bytes for action in actual] == [action.decision_bytes for action in expected]


# 回退软禁用按真实响应迁移到新池序号，末端未穷尽时不封禁主入口
def test_leaf_retry_follows_response_after_macro_pool_reorder():
    from tests.test_branch_retry import MAIN, snapshot
    leaf_actions = [native_types.GameAction(15, 0, operation_id=7, decision_bytes=CANCEL_RESPONSE),
        native_types.GameAction(15, 1, operation_id=20, decision_bytes=b"\1\0")]
    policy = CoreBranchRetry(threshold=1)
    main = snapshot(MAIN)
    policy.enter(main, 11, b"main")
    policy.stage(main, 0)
    leaf = snapshot(leaf_actions)
    policy.enter(leaf, 15, b"leaf")
    policy.stage(leaf, 0)
    returned = snapshot(MAIN)
    changes, _ = policy.enter(returned, 11, b"main")
    assert [item["message_type"] for item in changes] == [15]
    assert returned.core_soft_banned_choice_ids == ()
    policy.stage(returned, 0)
    reordered = snapshot(leaf_actions[::-1])
    for index, action in enumerate(reordered.valid_actions):
        action.index = index
    policy.enter(reordered, 15, b"leaf")
    assert reordered.core_soft_banned_choice_ids == (1,)
