# V4 在线兼容回归，覆盖真实后端决策历史掩蔽隐私边界及本机 TCP 操作闭环

import asyncio
from copy import deepcopy
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from agents.ai_bot import AiBot
from app_config import DecisionConfig
from core.gamestate import DuelState
from core.network import build_packet
from core.selection_protocol import validate_selection_response
from galatea_link import GalateaLink
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.live_adapter import prepare_online_input
from model_protocols.v4.online_adapter import project_online_snapshot
from tests.test_complex_selection import Reader, case_payload, state_with_cards
from tests.test_online_state import start_payload
from tests.test_v4_onnx_compatibility import frozen_graph
from tests.v4_fixtures import write_v4_assets, write_v4_checkpoint
from tests.v4_online_fixtures import full_query, make_state
from tests.online_server_fixtures import server_query_row, padded_code_row, confirmation_packet, chain_packet, reload_packet
from tests.test_message_layout_compatibility import wide_hint_packet


# 统一静态卡片元数据，测试不读取或修改外部 Core 仓库
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    monkeypatch.setattr("core.gamestate.card_db", Reader())


# 历史始终使用明确缺失的掩码，原始可见记录不会被修改或伪装成原生历史
@pytest.mark.parametrize("player", [0, 1])
def test_player_view_encodes_with_masked_transition_history(tmp_path, player):
    state, _ = make_state(player)
    source = state.get_snapshot()
    source.observed_transitions = (object(),)
    actions = deepcopy(source.valid_actions)
    encoder = GalateaEncoder(write_v4_assets(tmp_path / "assets"))
    arrays = encoder.encode(source, player)
    assert len(arrays) == 108 and not arrays["event_mask"].any()
    assert arrays["act_mask"].sum() == len(source.valid_actions)
    assert source.valid_actions == actions and len(source.observed_transitions) == 1
    assert encoder.last_online_report["ready"] is True
    assert encoder.last_online_report["history_policy"] == "masked_unavailable"
    assert encoder.last_online_report["native_observation_complete"] is False
    assert "transition_history_masked" in encoder.last_online_report["warnings"]
    assert not project_online_snapshot(source, player, history_projection=([], 0)).report["online_inference_ready"]


# 缺少真实查询的字段允许兼容输入，但仍把缺口保留为质量警告
def test_missing_dynamic_query_is_warning_instead_of_global_gate(tmp_path):
    state, _ = make_state()
    state.update(53, struct.pack("<IBBBBB", 14558127, 0, 4, 0, 1, 4))
    state.update(13, struct.pack("<BI", 0, 123))
    encoder = GalateaEncoder(write_v4_assets(tmp_path / "assets"))
    arrays = encoder.encode(state.get_snapshot(), 0)
    assert arrays["act_mask"].size == 120
    assert encoder.last_online_report["missing_dynamic_card_count"] > 0
    assert "cards_source_incomplete" in encoder.last_online_report["warnings"]


# 无 Start 行动者错位以及观战来源不能升级为对局玩家输入
def test_invalid_player_or_missing_start_stays_blocked(tmp_path):
    encoder = GalateaEncoder(write_v4_assets(tmp_path / "assets"))
    with pytest.raises(ValueError, match="Start|start_missing"):
        encoder.encode(DuelState().get_snapshot(), 0)
    state, _ = make_state()
    with pytest.raises(ValueError, match="切换"):
        encoder.encode(state.get_snapshot(), 1)
    assert not encoder.last_online_report["ready"]
    source = state.get_snapshot()
    source.global_data.to_play = 1
    prepared = prepare_online_input(source, 0)
    assert prepared.snapshot is None
    assert "decision_player_mismatch" in prepared.report["blocking_reasons"]


# 诊断投影继续隔离，不能绕过兼容策略直接交给模型
def test_diagnostic_projection_does_not_become_inference_input(tmp_path):
    state, _ = make_state()
    projection = project_online_snapshot(state.get_snapshot(), 0)
    with pytest.raises(ValueError, match="诊断投影"):
        GalateaEncoder(write_v4_assets(tmp_path / "assets")).encode(projection.snapshot, 0)


# 所有权不明的候选指针和超容量场面明确失败，不能给模型构造错误引用
@pytest.mark.parametrize("failure", ["target", "capacity", "empty_actions"])
def test_invalid_geometry_is_not_relaxed_by_compatibility(failure):
    state, _ = make_state()
    source = state.get_snapshot()
    if failure == "target":
        source.valid_actions[0].target_entity_idx = 999
    elif failure == "capacity":
        source.entities = [deepcopy(source.entities[0]) for _ in range(121)]
    else:
        source.valid_actions = []
    prepared = prepare_online_input(source, 0)
    assert prepared.snapshot is None and prepared.report["blocking_reasons"]


# 兼容映射只保留当前玩家实际可见身份，不能读取对手手牌或盖放卡
@pytest.mark.parametrize("zone,position", [(2, 1), (4, 8)])
def test_hidden_opponent_identity_is_masked(zone, position):
    state, _ = make_state()
    state.update(7, bytes([1, zone, 0]) + full_query(1, zone, 0, code=89631139,
        position=1 | zone << 8 | position << 24))
    source = state.get_snapshot()
    source.valid_actions = [deepcopy(source.valid_actions[0])] if source.valid_actions else []
    # 查询刷新不更换原菜单，沿用当前仍有效的基础动作
    prepared = prepare_online_input(source, 0)
    assert prepared.snapshot is not None
    for entity in prepared.snapshot.entities:
        if entity.owner == 1 and entity.location == zone:
            assert entity.code == 0 and not entity.is_public


# 已经公开的无序手牌记忆仍传给模型，清理隐藏槽位不应抹掉合法已知信息
def test_public_hand_memory_is_preserved_without_aliasing():
    state, _ = make_state()
    source = state.get_snapshot()
    source.known_hand_codes = {0: [], 1: [89631139]}
    prepared = prepare_online_input(source, 0)
    assert prepared.snapshot.known_hand_codes == {0: [], 1: [89631139]}
    source.known_hand_codes[1].clear()
    assert prepared.snapshot.known_hand_codes[1] == [89631139]


# 使用真实 PTH 与 ONNX 后端从在线快照获取决策，候选编号和原始响应仍保持对应
@pytest.mark.parametrize("backend", ["pytorch", "onnxruntime"])
@pytest.mark.parametrize("player", [0, 1])
def test_real_runtime_selects_online_action(tmp_path, backend, player):
    assets = write_v4_assets(tmp_path / "assets")
    path, _ = write_v4_checkpoint(tmp_path, assets) if backend == "pytorch" else frozen_graph(tmp_path, assets)
    bot = AiBot(initialize_network=False)
    assert bot.load_model(path, asset_path=assets.root, protocol="v4")
    assert bot.model_available and bot.model_metadata["online_inference_ready"]
    state, _ = make_state(player)
    decision = bot.get_scored_decision_from_snapshot(state.get_snapshot(), 11)
    assert decision is not None and 0 <= decision.choice_id < len(state.current_valid_actions)
    assert decision.response == bot.pack_choice_from_snapshot(state.get_snapshot(), decision.choice_id, 11)
    assert bot.encoder.last_online_report["ready"]


# 空场也有阶段和询问等合法动作，不能因为没有实体便绕过 V4
def test_empty_field_still_uses_v4_model(tmp_path):
    assets = write_v4_assets(tmp_path / "assets")
    path, _ = write_v4_checkpoint(tmp_path, assets)
    bot = AiBot(initialize_network=False)
    assert bot.load_model(path, asset_path=assets.root)
    state = DuelState(model_protocol_version=4)
    state.update(4, start_payload())
    state.update(40, b"\0")
    state.update(13, struct.pack("<BI", 0, 123))
    assert not state.get_snapshot().entities
    assert bot.get_scored_decision_from_snapshot(state.get_snapshot(), 13) is not None


# 本机 TCP 模拟开局连锁及连续三阶段提示，确认实际 V4 推理产出协议响应而不调用规则回退
@pytest.mark.asyncio
@pytest.mark.parametrize("backend,player", [("pytorch", 0), ("onnxruntime", 1)])
@pytest.mark.parametrize("opening", ["yes_no", "chain"])
async def test_v4_tcp_duel_loop_uses_model_across_prompt_stages(tmp_path, monkeypatch, backend, player, opening):
    assets = write_v4_assets(tmp_path / "assets")
    path, _ = write_v4_checkpoint(tmp_path, assets) if backend == "pytorch" else frozen_graph(tmp_path, assets)
    (tmp_path / "decks").mkdir()
    (tmp_path / "decks/test.ydk").write_text("#main\n14558127\n89631139\n#extra\n89631139\n!side\n", encoding="utf-8")
    monkeypatch.setattr("galatea_link.LINK_DATA_ROOT", tmp_path)
    monkeypatch.setattr("galatea_link.card_db.reload", lambda *args, **kwargs: None)
    loop = asyncio.get_running_loop()
    received = loop.create_future()
    release = asyncio.Event()
    payloads = []
    _, sorting, _ = case_payload("sort", player)

    # 读取一个标准 YGOPro 帧并保留实际响应字节
    async def read_packet(reader):
        size = struct.unpack("<H", await reader.readexactly(2))[0]
        packet = await reader.readexactly(size)
        return packet[0], packet[1:]

    # 模拟已接纳连接的服务器，保留真实 TCP 拆包收包及 Link 全部决策处理
    async def serve(reader, writer):
        try:
            while (await read_packet(reader))[0] != 0x12:
                pass
            writer.write(build_packet(0x13, bytes([player])))
            writer.write(build_packet(0x15))
            writer.write(build_packet(1, bytes([4]) + start_payload(player)))
            # 真实服务器刷新对手手牌时保留行长度并清零内容，推理必须能继续
            hidden_row = struct.pack("<I", 80) + bytes(76)
            writer.write(build_packet(1, bytes([6, 1 - player, 2]) + hidden_row * 5))
            # 模拟传统服务器 363 字节手牌刷新和 1203 字节额外刷新，包含无载荷历史位
            own_hand = b"".join(server_query_row(player, 2, index) for index in range(5))
            own_extra = b"".join(server_query_row(player, 64, index, flags=0xF81FFF, code=89631139, position=8) for index in range(15))
            writer.write(build_packet(1, bytes([6, player, 2]) + own_hand))
            writer.write(build_packet(1, bytes([6, player, 64]) + own_extra))
            writer.write(build_packet(1, bytes([40, player])))
            writer.write(build_packet(1, bytes([41]) + struct.pack("<H", 4)))
            for index, code in enumerate((14558127, 89631139, 46986414)):
                writer.write(build_packet(1, bytes([7, player, 4, index]) + full_query(player, 4, index, code=code)))
            first_prompt = chain_packet(player) if opening == "chain" else bytes([13]) + struct.pack("<BI", player, 123)
            writer.write(build_packet(1, first_prompt))
            await writer.drain()
            while len(payloads) < 3:
                kind, raw = await read_packet(reader)
                if kind != 1:
                    continue
                payloads.append(raw)
                if len(payloads) == 1:
                    assert struct.unpack("<i", raw)[0] in ((0, -1) if opening == "chain" else (0, 1))
                    # 复现首个决策之后对手手牌收到八十字节仅卡号刷新，连接仍需继续响应
                    writer.write(build_packet(1, bytes([7, 1 - player, 2, 0]) + padded_code_row()))
                    writer.write(build_packet(1, wide_hint_packet(player)))
                    writer.write(build_packet(1, confirmation_packet(player, skip_panel=1)))
                    # 强制标志属于候选，特殊候选数为零仍不允许返回取消
                    writer.write(build_packet(1, chain_packet(player, special_count=0,
                        candidates=[(0, 1, 14558127, player | 4 << 8 | 1 << 24, 0x16020304)])))
                    await writer.drain()
                elif len(payloads) == 2:
                    assert raw == struct.pack("<i", 0)
                    # 完整场面重建后补发本方身份查询，模型必须继续完成下一次排序响应
                    restored_players = [{'lp': 6000, 'monsters': [(index, 1, 0) for index in range(3)],
                        'spells': [], 'counts': (30, 5, 0, 0, 15, 0)} for _ in range(2)]
                    writer.write(build_packet(1, reload_packet(players=restored_players, chains=[])))
                    for index, code in enumerate((14558127, 89631139, 46986414)):
                        writer.write(build_packet(1, bytes([7, player, 4, index]) + full_query(player, 4, index, code=code)))
                    writer.write(build_packet(1, bytes([21]) + sorting))
                    await writer.drain()
                else:
                    validate_selection_response(21, sorting, raw)
            received.set_result(True)
            await release.wait()
        except BaseException as error:
            if not received.done():
                received.set_exception(error)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    link = GalateaLink("127.0.0.1", server.sockets[0].getsockname()[1], "", "test",
        weights_path=str(path), model_assets_path=str(assets.root), model_protocol="v4",
        decision_config=DecisionConfig(mode="core_only", core_time_budget=15.0))
    rule = AsyncMock(side_effect=AssertionError("V4 测试不应使用规则回退"))
    link._compute_rule_decision = rule
    events = []
    publish = link._publish_event
    # 在不替换事件总线的前提下记录决策来源
    def capture(kind, payload=None):
        events.append(kind)
        return publish(kind, payload)
    link._publish_event = capture
    task = asyncio.create_task(link.start())
    try:
        assert await asyncio.wait_for(asyncio.shield(received), 20)
        assert len(payloads) == 3 and events.count("core.suggested") == 3
        assert events.count("decision.committed") == 3
        assert events.count('model.feedback.updated') == 3
        feedback = link.get_model_feedback()
        assert feedback['available'] and len(feedback['history']) == 3
        assert feedback['latest']['source'] == 'decision_shared'
        assert (feedback['latest']['probabilities'] is not None) == (backend == 'pytorch')
        assert "message.processing_failed" not in events
        assert len(link.gamestate.field_map[1 - player][2]) == 5
        assert link.gamestate.online_projection.compatibility_warning_counts["query_extra_bytes"] == 1
        assert link.gamestate.online_projection.compatibility_warning_counts["message_wide_hint"] == 1
        assert link.gamestate.online_projection.confirmation_header_size == 3
        assert link.gamestate.online_projection.chain_entry_size == 14
        assert not link.gamestate.recently_confirmed
        assert link.gamestate.online_projection.field_rebuilds == 1
        rule.assert_not_awaited()
        assert link.ai.encoder.last_online_report["history_policy"] == "masked_unavailable"
        status = link.runtime.get_status()["core_model"]
        assert status["available"] and status["online_input"]["ready"]
        diagnostics = link.runtime.get_v4_diagnostics()
        assert diagnostics["online_inference_ready"] is False
        assert diagnostics["readiness_scope"] == "native_observation"
        assert diagnostics["online_compatibility"]["ready"] is True
    finally:
        release.set()
        await link.close()
        await asyncio.wait_for(task, 5)
        server.close()
        await server.wait_closed()
