# 复杂选择端到端回归，覆盖原始候选完整响应数值边界与只读 Core 对照

import ast
import asyncio
import io
import itertools
import os
from pathlib import Path
import struct
import threading
from dataclasses import replace
from functools import lru_cache
from unittest.mock import AsyncMock

import numpy as np
import pytest

from action_candidates import build_macro_action_pool
from agents.ai_bot import AiBot
from agents.decision_policy import DecisionOutcome
from core.observation import LlmObservationBuilder
from core.parser import OCGParser
from core.rule_bot import get_rule_decision
from core.selection_options import build_selection_options
from core.selection_protocol import (CANCEL_RESPONSE, SelectionProtocolError, SelectionSearchLimit,
    parse_selection_prompt, validate_selection_response)
from game_constants import LocationInfo
from core.gamestate import DuelState
from data_types import GameAction
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.online_adapter import project_online_snapshot
from tests.test_response_history import commit_link
from tests.v4_fixtures import write_v4_assets
from tests.v4_online_fixtures import FixtureCardReader, full_query, make_state, readonly_core_reference


CASES = ("multi", "tribute", "double_tribute", "counters", "exact", "mandatory_only", "greater", "dual_greater", "sort", "step")
CODES = (14558127, 89631139, 46986414)


class Reader(FixtureCardReader):
    # 测试名称不依赖本机卡库
    def get_card_name(self, code):
        return f"Card {code}"

    # 测试文本不涉及外部网络查询
    def get_card_text(self, code):
        return "测试文本"


# 固定静态元数据以便两侧编码仅比较协议与动态状态
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    monkeypatch.setattr("core.gamestate.card_db", Reader())


# 使用不同实例构造三个公开候选，不让同名副本合并掩盖物理索引错误
def state_with_cards(player):
    state, previous = make_state(player)
    rows = [row for row in previous if row[1] == 64]
    for index, code in enumerate(CODES):
        raw = full_query(player, 4, index, code=code)
        state.update(7, bytes([player, 4, index]) + raw)
        rows.append((player, 4, index, raw))
    return state, rows


# 独立构建网络样本，期望响应直接来自数量和值规则而不调用待测生成器
def case_payload(name, player):
    # 选卡的末字节是表示，祭品的末字节是解放值
    def card(index, extra=1):
        return struct.pack("<IBBBB", CODES[index], player, 4, index, extra)
    # 求和候选使用十一字节结构且可包含两个独立数值
    def material(index, value):
        return struct.pack("<IBBBI", CODES[index], player, 4, index, value)
    if name == "multi":
        payload = bytes([player, 1, 2, 2, 3]) + b"".join(card(index) for index in range(3))
        return 15, payload, {CANCEL_RESPONSE, b"\2\0\1", b"\2\0\2", b"\2\1\2"}
    if name in ("tribute", "double_tribute"):
        minimum, maximum = (3, 2) if name == "tribute" else (2, 1)
        payload = bytes([player, 1, minimum, maximum, 3]) + b"".join(card(index, value) for index, value in enumerate((2, 1, 1)))
        expected = {b"\2\0\1", b"\2\0\2"} if name == "tribute" else {b"\1\0"}
        return 20, payload, expected | {CANCEL_RESPONSE}
    if name == "counters":
        payload = struct.pack("<BHHB", player, 0x1234, 3, 3)
        payload += b"".join(struct.pack("<IBBBH", CODES[index], player, 4, index, available)
            for index, available in enumerate((1, 2, 3)))
        expected = {struct.pack("<HHH", *values) for values in itertools.product(range(2), range(3), range(4)) if sum(values) == 3}
        return 22, payload, expected
    if name == "exact":
        payload = struct.pack("<BBIBBB", 0, player, 6, 1, 2, 1) + material(0, 2)
        payload += b"\2" + material(1, 3 | 4 << 16) + material(2, 1 | 2 << 16)
        return 23, payload, {b"\2\0\0", b"\3\0\0\1"}
    if name == "mandatory_only":
        payload = struct.pack("<BBIBBB", 0, player, 2, 0, 0, 1) + material(0, 2) + b"\0"
        return 23, payload, {b"\1\0"}
    if name == "greater":
        payload = struct.pack("<BBIBBB", 1, player, 8, 1, 1, 0) + b"\3"
        payload += b"".join(material(index, value) for index, value in enumerate((3, 3, 2)))
        return 23, payload, {b"\3\0\1\2"}
    if name == "dual_greater":
        payload = struct.pack("<BBIBBB", 1, player, 10, 1, 1, 0) + b"\2"
        payload += material(0, 2 | 100 << 16) + material(1, 2 | 100 << 16)
        return 23, payload, {b"\1\0", b"\1\1", b"\2\0\1"}
    if name == "sort":
        payload = bytes([player, 3]) + b"".join(struct.pack("<IBBB", CODES[index], player, 4, index) for index in range(3))
        return 25, payload, {bytes(order) for order in itertools.permutations(range(3))}
    payload = bytes([player, 0, 1, 1, 2, 2])
    payload += b"".join(struct.pack("<II", CODES[index], LocationInfo.encode(player, 4, index, 0)) for index in (0, 1))
    payload += b"\1" + struct.pack("<II", CODES[2], LocationInfo.encode(player, 4, 2, 0))
    return 26, payload, {b"\1\0", b"\1\1", b"\1\2", CANCEL_RESPONSE}


# 完整候选应全部可发送，模型投影与 LLM 获得同一组已验证选择
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("name", CASES)
def test_complete_options_and_public_constraints(name, player):
    state, _ = state_with_cards(player)
    kind, payload, expected = case_payload(name, player)
    assert OCGParser.robust_parse(bytes([kind]) + payload + b"\x28\0", strict=True) == [(kind, payload), (40, b"\0")]
    state.update(kind, payload)
    if kind == 26:
        actions = state.current_valid_actions
    else:
        base = list(state.current_valid_actions)
        actions = build_macro_action_pool(kind, payload, state, base, np.ones(len(base)))
        state.current_valid_actions = actions
    source = state.get_snapshot()
    projection = project_online_snapshot(source, player, event_type=kind)
    assert projection.report["groups"]["actions"]["candidate_stage"] == ("direct_candidates" if kind == 26 else "explicit_responses")
    bot = AiBot(initialize_network=False)
    actual = {bytes(bot.pack_choice_from_snapshot(projection.snapshot, index, kind)) for index in range(len(actions))}
    assert actual == expected
    for response in actual:
        assert validate_selection_response(kind, payload, response) == response
    observation = LlmObservationBuilder(Reader()).build(source, player, event_type=kind)
    context = observation["selection_context"]
    if kind == 20:
        assert context["minimum_release_value"] == (3 if name == "tribute" else 2)
        assert all("selection_items" in action for action in observation["legal_actions"] if action["description"] != "Cancel")
    elif kind == 22:
        assert context["required_counters"] == 3 and context["counter_type"] == 0x1234
        assert all(sum(item["remove_count"] for item in action["selection_items"]) == 3 for action in observation["legal_actions"])
    elif kind == 23:
        assert CANCEL_RESPONSE not in actual
        assert context["optional_count_limits_apply"] == (name not in ("greater", "dual_greater"))
    elif kind == 25:
        assert context["order_length"] == 3
        for index, action in enumerate(source.valid_actions):
            response = bot.pack_choice_from_snapshot(source, index, kind)
            recovered = sorted(range(3), key=lambda original: response[original])
            assert action.macro_target_codes == [CODES[original] for original in recovered]
    elif kind == 26:
        assert context["selected_count"] == 1
        assert [action["selection_operation"] for action in observation["legal_actions"]] == ["cancel", "select", "select", "unselect"]
    assert not projection.report["online_inference_ready"]


# 四类新增基础提示和逐项选撤均执行 Core 原始解析与全部一百零八项输入比较
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("name", CASES)
def test_complex_base_inputs_match_readonly_core(tmp_path, name, player):
    state, rows = state_with_cards(player)
    kind, payload, _ = case_payload(name, player)
    state.update(kind, payload)
    reference = readonly_core_reference(os.environ["GALATEA_CORE_READONLY_ROOT"], state, rows, player, msg_type=kind, payload=payload)
    projection = project_online_snapshot(state.get_snapshot(), player, event_type=kind)
    assert projection.report["groups"]["actions"]["candidate_stage"] == ("direct_candidates" if kind == 26 else "base_candidates")
    report = GalateaEncoder(write_v4_assets(tmp_path / "assets")).audit_projection(projection, reference, player)
    assert report["all_fields_equal"], report["different_fields"]
    assert report["field_count"] == 108


# 只在内存编译 Core 选择校验器，既不导入仓库模块也不写入 Core
def readonly_validator(root):
    tree = ast.parse((Path(root) / "selection_protocol.py").read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    namespace = {"io": io, "struct": struct, "lru_cache": lru_cache}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<readonly Core selection validation>", "exec"), namespace)
    return namespace["validate_selection_response"]


# 新生成的索引选择全部通过独立 Core 校验，线上严格长度规则另行保留
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
def test_generated_responses_pass_native_selection_checks():
    validate = readonly_validator(os.environ["GALATEA_CORE_READONLY_ROOT"])
    for name in ("multi", "tribute", "double_tribute", "exact", "mandatory_only", "greater", "dual_greater", "step"):
        kind, payload, expected = case_payload(name, 0)
        for response in expected:
            assert validate(kind, payload, response) == response


# 异常长度索引分配和取消均须拒绝，不能发送本地填充缓冲区给服务器
def test_invalid_responses_are_rejected_before_transport():
    cases = [("multi", b"\2\0\0"), ("multi", b"\2\0\3"), ("multi", b"\1\0"),
        ("double_tribute", b"\1\1"), ("counters", b"\3\0\0\0\0\0"),
        ("counters", b"\0"), ("exact", CANCEL_RESPONSE), ("sort", b"\0\0\2"),
        ("step", b"\2\0\1"), ("multi", b"\2\0\1" + bytes(509))]
    for name, response in cases:
        kind, payload, _ = case_payload(name, 0)
        with pytest.raises(SelectionProtocolError):
            validate_selection_response(kind, payload, response)
    kind, payload, _ = case_payload("multi", 0)
    with pytest.raises(SelectionProtocolError):
        parse_selection_prompt(kind, payload + b"\0")
    with pytest.raises(SelectionProtocolError):
        parse_selection_prompt(kind, payload[:-1])


# 空排序和零指示物保留空字节响应，不能被模型打包器改成整数零
@pytest.mark.parametrize("kind,payload", [(25, b"\0\0"), (22, b"\0\1\0\0\0\0")])
def test_empty_response_stays_explicit_bytes(kind, payload):
    state, _ = make_state()
    state.update(kind, payload)
    state.current_valid_actions = build_macro_action_pool(kind, payload, state, [], [])
    projection = project_online_snapshot(state.get_snapshot(), 0, event_type=kind)
    assert AiBot(initialize_network=False).pack_choice_from_snapshot(projection.snapshot, 0, kind) == b""
    assert validate_selection_response(kind, payload, b"") == b""


# 规则回退与模型共享合法响应，硬拒绝耗尽后明确报错而不重发已拒绝动作
def test_rule_fallback_uses_valid_options_and_preserves_rejections():
    state, _ = state_with_cards(0)
    for name in CASES:
        kind, payload, expected = case_payload(name, 0)
        response = get_rule_decision(0, kind, bytes([kind]) + payload, state)
        assert response in expected
        with pytest.raises(SelectionProtocolError):
            get_rule_decision(0, kind, bytes([kind]) + payload, state, list(expected))


# 搜索达到小预算可显式失败，不能转成假取消或长期穷举
def test_search_is_bounded_when_no_candidate_is_valid():
    _, payload, _ = case_payload("greater", 0)
    payload = payload[:2] + struct.pack("<I", 0x7FFFFFFF) + payload[6:]
    with pytest.raises(SelectionSearchLimit):
        build_selection_options(23, payload, search_budget=1)


# 提交边界拒绝求和取消，不发布已提交事件也不向网络写入
@pytest.mark.asyncio
async def test_invalid_final_response_never_reaches_game_connection():
    link, request, _ = commit_link(AsyncMock(return_value=True))
    state, _ = state_with_cards(0)
    kind, payload, _ = case_payload("exact", 0)
    state.update(kind, payload)
    request = replace(request, msg_type=kind, raw_msg=bytes([kind]) + payload, snapshot=state.get_snapshot())
    with pytest.raises(SelectionProtocolError):
        await link._commit_decision(request, DecisionOutcome(response=-1, source="rule"))
    link.client.send_decision.assert_not_awaited()
    link._publish_event.assert_not_called()


# 同名墓地实例以及已知状态不同的手牌副本不能被候选合并吞掉
@pytest.mark.parametrize("zone", [2, 16])
def test_known_distinct_copies_keep_separate_network_indices(zone):
    state = DuelState()
    for index in range(2):
        state.field_map[0][zone][index] = {"code": CODES[0], "pos": 1, "status_mask": index}
    payload = bytes([0, 0, 1, 1, 2])
    payload += b"".join(struct.pack("<IBBBB", CODES[0], 0, zone, index, 1) for index in range(2))
    options = build_selection_options(15, payload, state)
    assert {option["bytes"] for option in options} == {b"\1\0", b"\1\1"}


# Type26 保留索引二百五十五，超过可寻址上限时明确拒绝而非伪装成取消
def test_step_indices_respect_byte_capacity():
    header = bytes([0, 1, 0, 0, 255, 255])
    rows = b"".join(struct.pack("<II", 1, LocationInfo.encode(0, 2, index, 0)) for index in range(255))
    last = struct.pack("<II", 1, LocationInfo.encode(0, 2, 255, 0))
    payload = header + rows + b"\1" + last
    assert validate_selection_response(26, payload, b"\1\xff") == b"\1\xff"
    with pytest.raises(SelectionProtocolError):
        parse_selection_prompt(26, header + rows + b"\2" + last * 2)
    with pytest.raises(ValueError, match="八位"):
        AiBot(initialize_network=False)._pack_response(GameAction(26, 256), 26)


# 查询刷新保持原提示的菜单类型和共享约束，不把查询号当作新的选择类型
def test_query_refresh_keeps_selection_menu_semantics():
    state, _ = state_with_cards(0)
    kind, payload, _ = case_payload("counters", 0)
    state.update(kind, payload)
    base = list(state.current_valid_actions)
    state.current_valid_actions = build_macro_action_pool(kind, payload, state, base, np.ones(len(base)))
    state.update(7, bytes([0, 4, 0]) + full_query(0, 4, 0))
    observation = LlmObservationBuilder(Reader()).build(state.get_snapshot(), 0, event_type=7)
    assert observation["event"]["type"] == 7
    assert all(item["action_name"] == "select_counter" for item in observation["legal_actions"])
    assert observation["selection_context"]["required_counters"] == 3


# 求和复核离开事件循环执行，取消时不得补发已经失效的响应
@pytest.mark.asyncio
async def test_sum_validation_does_not_block_cancellation_or_send_late(monkeypatch):
    link, request, _ = commit_link(AsyncMock(return_value=True))
    state, _ = state_with_cards(0)
    kind, payload, _ = case_payload("exact", 0)
    state.update(kind, payload)
    request = replace(request, msg_type=kind, raw_msg=bytes([kind]) + payload, snapshot=state.get_snapshot())
    loop = asyncio.get_running_loop()
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    # 模拟线程内正在复核复杂数值，主循环应始终可取消任务
    def validate(*args):
        loop.call_soon_threadsafe(started.set)
        try:
            release.wait(2)
            return b"\2\0\0"
        finally:
            loop.call_soon_threadsafe(finished.set)
    monkeypatch.setattr("galatea_link.validate_selection_response", validate)
    task = asyncio.create_task(link._commit_decision(request, DecisionOutcome(response=b"\2\0\0", source="core")))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        link.client.send_decision.assert_not_awaited()
        link._publish_event.assert_not_called()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 2)


# 指示物数量独立使用十六位小端数，超过二百五十五时不截断
def test_counter_response_preserves_sixteen_bit_quantities():
    payload = struct.pack("<BHHBIBBBH", 0, 5, 300, 1, CODES[0], 0, 4, 0, 500)
    options = build_selection_options(22, payload)
    assert [option["bytes"] for option in options] == [b"\x2c\x01"]
    assert options[0]["values"] == [300]
    assert validate_selection_response(22, payload, options[0]["bytes"]) == b"\x2c\x01"


# 小状态穷举对照零值和双值边界，防止求和复刻只在手选样本上正确
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需显式指定只读 Core")
def test_sum_edge_space_agrees_with_readonly_validator():
    native = readonly_validator(os.environ["GALATEA_CORE_READONLY_ROOT"])
    # 比较合法性而不依赖两侧错误文字或异常类名
    def accepts(validator, payload, response):
        try:
            validator(23, payload, response)
            return True
        except ValueError:
            return False
    for mode in (0, 1):
        for count in range(1, 4):
            for values in itertools.product((0, 1, 3, 2 | 5 << 16), repeat=count):
                rows = b"".join(struct.pack("<IBBBI", 1, 0, 4, index, value) for index, value in enumerate(values))
                for target in range(9):
                    payload = struct.pack("<BBIBBBB", mode, 0, target, 0, count, 0, count) + rows
                    for size in range(count + 1):
                        for indices in itertools.combinations(range(count), size):
                            response = bytes([size, *indices])
                            assert accepts(validate_selection_response, payload, response) == accepts(native, payload, response)
