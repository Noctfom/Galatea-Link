# 卡片确认协议回归，覆盖真实面板标志旧版消息头和只读 Core 对照

import ast
import io
import os
from pathlib import Path
import struct

import pytest

from core.confirmation_protocol import parse_online_confirmation
from core.gamestate import DuelState
from core.observation import LlmObservationBuilder
from core.parser import OCGParser
from tests.test_online_state import start_payload
from tests.online_server_fixtures import confirmation_packet


# 十一字节帧只包含一条 Type31，不能产生伪 Retry 或 Type29
@pytest.mark.parametrize("player", [0, 1])
@pytest.mark.parametrize("skip_panel", [0, 1])
def test_panel_flag_confirmation_is_not_split_into_card_bytes(player, skip_panel):
    packet = confirmation_packet(player, skip_panel=skip_panel)
    assert len(packet) == 11
    if skip_panel == 0:
        assert packet[3:5] == bytes([1, 29])
    messages = OCGParser.robust_parse(packet, strict=True, compatibility=True)
    assert messages == [(31, packet[1:])]
    parsed = parse_online_confirmation(31, messages[0][1])
    assert parsed["header_size"] == 3 and parsed["skip_panel"] == bool(skip_panel)
    state = DuelState(model_protocol_version=4)
    state.update(4, start_payload(player))
    state.update(31, messages[0][1])
    assert state.recently_confirmed == [0x0102031D]
    assert state.online_projection.confirmation_header_size == 3
    observation = LlmObservationBuilder().build(state.get_snapshot(), player)
    assert any(item["code"] == 0x0102031D for item in observation["card_catalog"])


# 两类头部的零张和多张列表均保持完整边界，skip_panel 不控制信息公开
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("count", [0, 1, 3])
def test_confirmation_counts_and_following_prompt(legacy, count):
    cards = [(0x0102031D + index, 0, 1, index) for index in range(count)]
    packet = confirmation_packet(legacy=legacy, skip_panel=1, cards=cards)
    prompt = struct.pack("<BI", 0, 123)
    messages = OCGParser.robust_parse(packet + bytes([13]) + prompt, strict=True, compatibility=True)
    assert messages == [(31, packet[1:]), (13, prompt)]
    state = DuelState()
    state.update(4, start_payload())
    state.update(13, prompt)
    actions = list(state.current_valid_actions)
    state.update(31, messages[0][1])
    assert state.current_valid_actions == actions
    known = LlmObservationBuilder().build(state.get_snapshot(), 0)["public_context"]["known_deck_positions"]
    assert {item["sequence_from_bottom"] for item in known} == set(range(count))


# 旧版卡号低位与面板标志相同时仍按已识别布局拆分，避免无意跳过后续回合
@pytest.mark.parametrize("low_byte", [0, 1])
def test_learned_legacy_layout_preserves_ambiguous_merged_frames(low_byte):
    packet = confirmation_packet(legacy=True, cards=[(0x01020300 + low_byte, 0, 1, 0)])
    assert OCGParser.robust_parse(packet + bytes([40, 1]), strict=True, compatibility=True,
        confirmation_header_size=2) == [(31, packet[1:]), (40, b"\1")]


# 在内存执行 Core 的公开解析函数和长度函数，避免导入 Core 或生成缓存文件
@pytest.mark.parametrize("skip_panel", [0, 1])
def test_readonly_core_type31_parser_and_length_are_already_correct(skip_panel):
    root = os.environ.get("GALATEA_CORE_READONLY_ROOT")
    if not root:
        pytest.skip("未提供只读 Core 参考目录")
    namespace = {"io": io, "struct": struct}
    protocol = ast.parse((Path(root) / "core_message_protocol.py").read_text(encoding="utf-8"))
    names = {"CoreMessageProtocolError", "_read_exact", "read_confirm_cards", "parse_confirm_cards", "read_chain_prompt", "read_reload_field"}
    nodes = [node for node in protocol.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<readonly Core Type31>", "exec"), namespace)
    tree = ast.parse((Path(root) / "gamestate.py").read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MessageParser")
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name == "calculate_dynamic_length"]
    cls = ast.ClassDef(name="CoreMessageParser", bases=[], keywords=[], body=methods, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<readonly Core Type31 length>", "exec"), namespace)
    payload = confirmation_packet(skip_panel=skip_panel)[1:]
    expected = namespace["parse_confirm_cards"](payload)
    actual = parse_online_confirmation(31, payload)
    assert {key: actual[key] for key in ("player", "skip_panel", "cards")} == expected
    stream = io.BytesIO(payload + bytes([40, 0]))
    assert namespace["CoreMessageParser"].calculate_dynamic_length(31, stream) == len(payload)
    assert stream.tell() == 0
