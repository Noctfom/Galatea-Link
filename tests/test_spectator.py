# 观众席回归，覆盖公开投影真实模型只读发包动态参照与中途场面重建

import asyncio
import json
import struct
from unittest.mock import Mock

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from app_config import DecisionConfig
from core.gamestate import DuelState
from core.network import SPECTATOR_BLOCKED_REQUESTS, YgoNetClient, build_packet
from core.observation import LlmObservationBuilder
from galatea_link import GalateaLink
from model_protocols.v4.encoder import GalateaEncoder
from tests.test_reload_field_protocol import reload_packet
from tests.test_v4_onnx_compatibility import frozen_graph
from tests.v4_fixtures import write_v4_assets, write_v4_checkpoint
from tests.v4_online_fixtures import FixtureCardReader, full_query
from app_config import ServiceConfig
from service.http_api import create_http_app
from service.session_manager import LinkSessionManager


# 使用固定卡库并缩短采样间隔，测试不连接真实服务器
@pytest.fixture(autouse=True)
def fixed_environment(monkeypatch):
    monkeypatch.setattr('core.gamestate.card_db', FixtureCardReader())
    monkeypatch.setattr('galatea_link.FEEDBACK_SAMPLE_INTERVAL', 0.001)


# 构造只含协议基线的观众Start，交换标记不代表认领玩家座位
def observer_start(marker=0x10):
    return struct.pack('<BBIIHHHH', marker, 5, 8000, 7000, 40, 15, 40, 15)


# 逐包读取完整客户端请求，便于断言观众没有发出游戏响应
async def read_request(reader):
    size, = struct.unpack('<H', await reader.readexactly(2))
    data = await reader.readexactly(size)
    return data[0], data[1:]


# 公开投影同时遮蔽双方手牌与私有构筑，不因选参照玩家而恢复身份
@pytest.mark.parametrize('reference', [0, 1])
def test_spectator_projection_and_real_v4_input_are_public(tmp_path, reference):
    state = DuelState(spectator=True, p0_main=[89631139], p1_main=[89631139], model_protocol_version=4)
    state.update(4, observer_start(0x11))
    state.update(40, b'\0')
    for player in (0, 1):
        state.update(7, bytes([player, 2, 0]) + full_query(player, 2, 0, code=89631139))
        state.update(7, bytes([player, 4, 0]) + full_query(player, 4, 0, code=14558127))
    snapshot = state.get_snapshot()
    assert snapshot.online_coverage['perspective'] is None
    assert not snapshot.valid_actions and not snapshot.p0_initial_deck_codes and not snapshot.p1_initial_deck_codes
    assert all(card.code == 0 for card in snapshot.entities if card.location == 2)
    view = LlmObservationBuilder().build(snapshot, reference)
    assert view['perspective']['role'] == 'spectator' and view['perspective']['player_id'] is None
    assert view['perspective']['reference_player_id'] == reference
    assert not view['decision_required'] and not view['legal_actions']
    assert '89631139' not in json.dumps(view)
    assert all(card['visibility'] == 'hidden' for card in view['cards'] if card['location_id'] == 2)
    encoder = GalateaEncoder(write_v4_assets(tmp_path / 'assets'))
    arrays = encoder.encode_online(snapshot, reference, state_evaluation=True)
    assert not arrays['act_mask'].any() and not arrays['deck_film_mask'].any()
    assert not arrays['deck_profile_mask'].any() and not arrays['event_mask'].any()
    assert encoder.last_online_report['mode'] == 'spectator_public_compatible_v1'
    with pytest.raises(ValueError, match='独立状态评测'):
        encoder.encode_online(snapshot, reference)


# 观众基线不能从普通玩家Start取得私有视角，真实重建可作为中途加入的公开基线
def test_spectator_requires_actual_public_baseline_and_ignores_prompts():
    state = DuelState(spectator=True)
    state.update(4, observer_start(0))
    assert not state.online_projection.started
    state.update(162, reload_packet()[1:])
    assert not state.online_projection.started
    state.online_projection.spectator_confirmed = True
    state.update(162, reload_packet()[1:])
    assert state.online_projection.started and state.online_projection.start_source == 'reload_field'
    for player in (0, 1):
        state.update(13, struct.pack('<BI', player, 123))
    state.update(1, b'')
    assert not state.current_valid_actions and state.response_history.perspective is None


# 发包器本身也拒绝观众的游戏操作，避免其他调用入口绕过上层隔离
@pytest.mark.asyncio
async def test_spectator_transport_rejects_game_mutations_before_any_write():
    client = YgoNetClient('127.0.0.1', 1, Mock(), spectator=True)
    writer = Mock()
    client.writer, client.is_connected = writer, True
    for kind in SPECTATOR_BLOCKED_REQUESTS:
        with pytest.raises(RuntimeError, match='观众'):
            await client.send_packet(kind)
    writer.write.assert_not_called()


# 真实TCP观战通过PTH及新旧ONNX评测，切换参照不混合曲线且始终零游戏响应
@pytest.mark.asyncio
@pytest.mark.parametrize('backend,reference', [('pytorch', 0), ('onnx15', 1), ('onnx14', 0)])
@pytest.mark.parametrize('baseline', ['start', 'reload'])
async def test_real_spectator_tcp_inference_and_reference_switch(tmp_path, monkeypatch, backend, reference, baseline):
    assets = write_v4_assets(tmp_path / 'assets')
    path, _ = write_v4_checkpoint(tmp_path, assets) if backend == 'pytorch' else frozen_graph(tmp_path, assets,
        schema_revision=14 if backend == 'onnx14' else 15)
    monkeypatch.setattr('galatea_link.LINK_DATA_ROOT', tmp_path)
    monkeypatch.setattr('galatea_link.card_db.reload', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('galatea_link.load_deck', Mock(side_effect=AssertionError('观众不应读取卡组')))
    release, feedback_ready, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests, events = [], []
    server_error = asyncio.get_running_loop().create_future()

    # 服务器先分配临时玩家席，确认切观众后再发送公开数据
    async def serve(reader, writer):
        try:
            for expected in (0x10, 0x12):
                kind, _ = await read_request(reader)
                requests.append(kind)
                assert kind == expected
            writer.write(build_packet(0x12, bytes(20)) + build_packet(0x13, b'\1'))
            await writer.drain()
            kind, _ = await read_request(reader)
            requests.append(kind)
            assert kind == 0x21
            # 即使观众持有房主标志或收到玩家询问，也不准备开局或回复动作
            writer.write(build_packet(0x13, b'\x17'))
            writer.write(build_packet(0x21, b'\x09') + build_packet(0x21, b'\x19'))
            writer.write(build_packet(3) + build_packet(4) + build_packet(0x18, struct.pack('<BBH', 0, 0, 200)))
            if baseline == 'start':
                writer.write(build_packet(0x15) + build_packet(1, b'\4' + observer_start(0x11)))
            else:
                writer.write(build_packet(1, reload_packet()))
            writer.write(build_packet(1, bytes([40, 0])))
            for player in (0, 1):
                # 夹具故意在手牌查询里带卡号，公开投影仍须独立遮蔽两边
                writer.write(build_packet(1, bytes([7, player, 2, 0]) + full_query(player, 2, 0, code=89631139)))
                writer.write(build_packet(1, bytes([7, player, 4, 0]) + full_query(player, 4, 0)))
                writer.write(build_packet(1, b'\r' + struct.pack('<BI', player, 123)))
            await writer.drain()
            await release.wait()
            writer.write(build_packet(1, bytes([5, 1, 1])) + build_packet(0x16))
            await writer.drain()
            # 结束前继续收取所有输出，不能只检查某个上层决策计数
            while True:
                kind, _ = await read_request(reader)
                requests.append(kind)
        except asyncio.IncompleteReadError:
            pass
        except BaseException as error:
            if not server_error.done():
                server_error.set_result(error)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    link = GalateaLink('127.0.0.1', server.sockets[0].getsockname()[1], '', 'does-not-exist',
        weights_path=str(path), model_assets_path=str(assets.root), session_role='spectator',
        decision_config=DecisionConfig(mode='hybrid', core_time_budget=10,
            core_feedback_passive_enabled=True, core_feedback_spectator_player=reference))
    original_publish = link._publish_event

    # 使用真实事件总线同步采样和终局，避免定时睡眠猜测状态
    def capture(kind, payload=None):
        events.append(kind)
        if kind == 'model.feedback.updated':
            feedback_ready.set()
        if kind == 'duel.ended':
            ended.set()
        return original_publish(kind, payload)

    link._publish_event = capture
    task = asyncio.create_task(link.start())
    try:
        await asyncio.wait_for(feedback_ready.wait(), 15)
        assert not server_error.done(), server_error.result() if server_error.done() else None
        assert not link.deck and link.ai_core_player_id is None
        assert link.runtime.get_status()['session_role'] == 'spectator'
        observation = link.get_latest_llm_observation()
        assert observation['own_deck_identity'] is None and not observation['decision_required']
        assert observation['perspective']['player_id'] is None and '89631139' not in json.dumps(observation)
        report = link.get_model_feedback()
        assert report['latest']['source'] == 'spectator' and report['latest']['perspective'] == reference
        assert report['latest']['visibility'] == 'spectator_public'
        assert np.isfinite(report['latest']['discounted_value'])
        assert (report['latest']['probabilities'] is not None) == (backend == 'pytorch')
        series = report['series_id']
        feedback_ready.clear()
        await link.runtime.update_controls({'intervention': {'core_feedback_spectator_player': 1 - reference}})
        await asyncio.wait_for(feedback_ready.wait(), 15)
        switched = link.get_model_feedback()
        assert switched['series_id'] != series
        assert all(point['perspective'] == 1 - reference for point in switched['history'])
        release.set()
        await asyncio.wait_for(ended.wait(), 5)
        assert link.last_duel_result['outcome'] == 'observed_win'
        assert link.last_duel_result['winner_core_player_id'] == 1 and link.last_duel_result['self_core_player_id'] is None
        assert not ({'core.suggested', 'decision.committed', 'message.processing_failed'} & set(events))
    finally:
        release.set()
        await link.close()
        await asyncio.wait_for(task, 5)
        server.close()
        await server.wait_closed()
    assert set(requests) == {0x10, 0x12, 0x21}


# 无卡组的观战身份与参照设置可离线持久化，决斗身份继续要求合法卡组
@pytest.mark.asyncio
async def test_spectator_http_configuration_controls_and_validation(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('{}\n', encoding='utf-8')
    manager = LinkSessionManager(config)
    client = TestClient(TestServer(create_http_app(manager, ServiceConfig(api_token='test-token'))))
    await client.start_server()
    headers = {'Authorization': 'Bearer test-token'}
    try:
        assert (await client.get('/api/v1/model-feedback')).status == 401
        response = await client.patch('/api/v1/configuration', headers=headers,
            json={'patch': {'server': {'role': 'spectator'}, 'agent': {'deck': ''}}})
        assert response.status == 200, await response.text()
        response = await client.patch('/api/v1/controls', headers=headers,
            json={'patch': {'intervention': {'core_feedback_spectator_player': 1, 'core_feedback_passive_enabled': True}}})
        assert response.status == 200, await response.text()
        for value in (True, 2, '0', None):
            response = await client.patch('/api/v1/controls', headers=headers,
                json={'patch': {'intervention': {'core_feedback_spectator_player': value}}})
            assert response.status == 400, await response.text()
        response = await client.get('/api/v1/capabilities', headers=headers)
        payload = await response.json()
        assert payload['data']['online_observation']['spectator_seat']
        fresh = LinkSessionManager(config)
        assert fresh.get_configuration()['server']['role'] == 'spectator'
        assert fresh.get_controls()['intervention']['core_feedback_spectator_player'] == 1
        session = await fresh.configure_session({'server': {'role': 'spectator'}, 'agent': {'deck': ''}})
        assert session['server']['role'] == 'spectator' and session['agent']['deck'] == ''
        with pytest.raises((ValueError, FileNotFoundError)):
            await fresh.configure_session({'server': {'role': 'duelist'}, 'agent': {'deck': ''}})
        await fresh.close()
    finally:
        await client.close()


# 仅公开观察不依赖可用模型，缺少模型时保留观战状态且不启动规则回退
@pytest.mark.asyncio
async def test_basic_spectator_without_model_does_not_require_deck(tmp_path, monkeypatch):
    monkeypatch.setattr('galatea_link.LINK_DATA_ROOT', tmp_path)
    monkeypatch.setattr('galatea_link.card_db.reload', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('galatea_link.load_deck', Mock(side_effect=AssertionError('不应读取卡组')))
    link = GalateaLink('127.0.0.1', 7911, '', '', weights_path=str(tmp_path / 'missing.onnx'), session_role='spectator')
    try:
        await link.handle_server_msg(1, b'\4' + observer_start())
        assert link.is_spectator and link.duel_active and link.deck is None
        assert link.get_latest_llm_observation()['perspective']['role'] == 'spectator'
        assert not link.get_model_feedback()['model_available']
        assert link.remote_decisions.get_pending() is None
    finally:
        await link.close()
