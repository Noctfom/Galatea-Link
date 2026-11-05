# 在线连锁协议回归，覆盖伪 Type22 崩溃强制语义旧布局及只读 Core 对照

import ast
import io
import os
from pathlib import Path
import struct

import pytest

from agents.ai_bot import AiBot
from core.chain_protocol import chain_payload_length, parse_online_chain
from core.gamestate import DuelState, MessageParser
from core.observation import LlmObservationBuilder
from core.parser import OCGParseError, OCGParser
from core.rule_bot import get_rule_decision
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.online_adapter import project_online_snapshot
from tests.online_server_fixtures import chain_packet
from tests.test_complex_selection import Reader, state_with_cards
from tests.test_online_state import start_payload
from tests.v4_fixtures import write_v4_assets
from tests.v4_online_fixtures import readonly_core_reference


# 固定静态卡片资料，不依赖本机卡库内容
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    monkeypatch.setattr("core.gamestate.card_db", Reader())


# 二十六字节标准连锁只能拆出一条消息，不能把末尾描述字节误认作 Type22
@pytest.mark.parametrize("player", [0, 1])
def test_26_byte_chain_does_not_create_select_counter(player):
    packet = chain_packet(player)
    assert len(packet) == 26 and packet[-1] == 22
    with pytest.raises(OCGParseError) as previous:
        OCGParser._split_messages(packet, chain_entry_size=13)
    assert previous.value.layout_context["offset"] == 25
    assert previous.value.layout_context["failed_type"] == 22
    assert OCGParser.robust_parse(packet, strict=True, compatibility=True) == [(16, packet[1:])]
    state = DuelState(model_protocol_version=4)
    state.update(4, start_payload(player))
    state.update(16, packet[1:])
    actions = state.current_valid_actions
    assert [action.index for action in actions] == [0, -1]
    assert actions[0].code == 14558127 and actions[0].desc_id == 0x16020304
    assert actions[0].target_location_raw == player | 4 << 8 | 1 << 24
    assert actions[0].context_value == 1 and actions[0].prompt_flags == 0
    assert actions[0].prompt_value == 0x01020304 and actions[0].prompt_value2 == 0x05060708
    assert state.online_projection.chain_entry_size == 14
    assert state.online_projection.coverage()["chain_entry_size"] == 14
    actions_report = project_online_snapshot(state.get_snapshot(), player, event_type=16).report['groups']['actions']
    assert actions_report['source_complete'] and not actions_report['legacy_chain_semantics_unverified']
    observation = LlmObservationBuilder(Reader()).build(state.get_snapshot(), player, event_type=16)
    first, cancel = observation["legal_actions"]
    assert not first["forced"] and first["cancelable"] and first["special_count"] == 1
    assert cancel["chain_operation"] == "cancel"


# 特殊候选数不能替代强制标志，每个候选保留自己的模式且只要有强制项就不提供取消
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("forced", [0, 1])
def test_per_candidate_flags_and_rule_fallback_share_semantics(monkeypatch, player, forced):
    candidates = [(0, 0, 14558127, player | 4 << 8, 123),
        (2, forced, 89631139, player | 4 << 8 | 1 << 16, 456)]
    packet = chain_packet(player, special_count=0x7F, candidates=candidates, timing=1)
    state = DuelState()
    state.update(4, start_payload(player))
    state.update(16, packet[1:])
    expected = [0, 1] if forced else [0, 1, -1]
    actions = state.current_valid_actions
    assert [action.index for action in actions] == expected
    assert [action.prompt_flags for action in actions[:2]] == [0, 2 | forced << 8]
    assert all(action.cancelable == (not forced) and action.context_value == 0x7F for action in actions)
    choices = []
    # 记录规则回退候选，确认它不把提示时机最低字节当作强制标志
    def select_last(items):
        choices.extend(items)
        return items[-1]
    monkeypatch.setattr("core.rule_bot.random.choice", select_last)
    assert get_rule_decision(player, 16, packet, state, valid_actions=[]) == expected[-1]
    assert choices == expected
    bot = AiBot(initialize_network=False)
    assert [bot.pack_choice_from_snapshot(state.get_snapshot(), index, 16)
        for index in range(len(actions))] == expected


# 两种布局的空及多候选消息保留后续指令，空连锁不能误记为已识别旧协议
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("count", [0, 1, 3])
def test_chain_counts_and_following_message(legacy, count):
    candidates = [(index % 3, 0, 14558127 + index, 0x01000400 | index << 16, 123 + index)
        for index in range(count)]
    packet = chain_packet(legacy=legacy, candidates=candidates)
    preferred = 13 if legacy else 14
    messages = OCGParser.robust_parse(packet + bytes([40, 1]), strict=True,
        chain_entry_size=preferred)
    assert messages == [(16, packet[1:]), (40, b"\1")]
    state = DuelState()
    state.update(4, start_payload())
    state.update(16, packet[1:])
    assert state.online_projection.chain_entry_size == (preferred if count else None)
    stream = io.BytesIO(packet[1:])
    assert MessageParser.calculate_dynamic_length(16, stream) == len(packet) - 1
    assert stream.tell() == 0


# 旧版全局强制标志仍禁止取消，并且已识别布局能处理卡号低字节零或一的歧义
@pytest.mark.parametrize("low_byte", [0, 1])
def test_legacy_global_forced_and_learned_layout(low_byte):
    packet = chain_packet(legacy=True, legacy_forced=1,
        candidates=[(0, 0, 0x01020300 + low_byte, 0x01000400, 123)])
    assert OCGParser.robust_parse(packet + bytes([40, 1]), strict=True, chain_entry_size=13) == [
        (16, packet[1:]), (40, b"\1")]
    prompt = parse_online_chain(packet[1:])
    assert prompt["forced"] and prompt["candidates"][0]["forced"]
    state = DuelState()
    state.update(4, start_payload())
    state.update(16, packet[1:])
    assert [action.index for action in state.current_valid_actions] == [0]


# 真正截断的列表和数量不符仍报告边界错误，不能用跳字节方式继续运行
def test_truncated_chain_is_not_silently_skipped():
    packet = chain_packet(candidates=[(0, 1, 14558127, 0x01000400, 123)] * 2)
    for damaged in (packet[:9], packet[:-3]):
        with pytest.raises(OCGParseError):
            OCGParser.robust_parse(damaged, strict=True, compatibility=True)
    with pytest.raises(ValueError):
        parse_online_chain(packet[1:-1])
    with pytest.raises(ValueError):
        chain_payload_length(packet[1:-1], preferred_entry_size=14)


# 只读提取 Core 当前字段长度及动作解析，逐项对照 V4 的全部输入而不修改 Core
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("forced", [0, 1])
def test_readonly_core_chain_parser_and_v4_inputs_are_already_correct(tmp_path, player, forced):
    root = os.environ.get("GALATEA_CORE_READONLY_ROOT")
    if not root:
        pytest.skip("未提供只读 Core 参考目录")
    namespace = {"io": io, "struct": struct}
    protocol = ast.parse((Path(root) / "core_message_protocol.py").read_text(encoding="utf-8"))
    names = {"CoreMessageProtocolError", "_read_exact", "read_chain_prompt", "parse_chain_prompt",
        "read_confirm_cards", "read_reload_field"}
    nodes = [node for node in protocol.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<readonly Core Type16>", "exec"), namespace)
    tree = ast.parse((Path(root) / "gamestate.py").read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MessageParser")
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name == "calculate_dynamic_length"]
    cls = ast.ClassDef(name="CoreMessageParser", bases=[], keywords=[], body=methods, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<readonly Core Type16 length>", "exec"), namespace)
    state, rows = state_with_cards(player)
    candidates = [(0, 0, 14558127, player | 4 << 8 | 1 << 24, 123),
        (2, forced, 89631139, player | 4 << 8 | 1 << 16 | 1 << 24, 456)]
    payload = chain_packet(player, special_count=0x7F, candidates=candidates)[1:]
    actual = parse_online_chain(payload)
    assert {key: value for key, value in actual.items() if key != "entry_size"} == namespace["parse_chain_prompt"](payload)
    stream = io.BytesIO(payload + bytes([40, 0]))
    assert namespace["CoreMessageParser"].calculate_dynamic_length(16, stream) == len(payload)
    assert stream.tell() == 0
    state.update(16, payload)
    reference = readonly_core_reference(root, state, rows, player, msg_type=16, payload=payload)
    projection = project_online_snapshot(state.get_snapshot(), player, event_type=16)
    report = GalateaEncoder(write_v4_assets(tmp_path / "assets")).audit_projection(projection, reference, player)
    assert report["field_count"] == 108
    assert report["all_fields_equal"], report["different_fields"]
