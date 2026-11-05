# 局内模型反馈回归，覆盖同次推理复用无动作评测降级权限和异步过期丢弃

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import struct

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agents.ai_bot import AiBot
from agents.decision_policy import InterventionPolicy
from app_config import DecisionConfig, ServiceConfig
from core.model_feedback import ModelFeedbackHistory, serialize_model_evaluation
from galatea_link import GalateaLink
from service.http_api import create_http_app
from service.session_manager import LinkSessionManager
from tests.test_complex_selection import Reader
from tests.test_v4_onnx_compatibility import frozen_graph
from tests.v4_fixtures import write_v4_assets, write_v4_checkpoint
from tests.v4_online_fixtures import make_state


# 固定测试卡片资料，避免使用本机私有卡库
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    monkeypatch.setattr('core.gamestate.card_db', Reader())


# 胜平负来自独立三分类头，折扣价值即使越界也不被换算为胜率
def test_terminal_probabilities_are_not_derived_from_critic():
    result = serialize_model_evaluation([[99]], {'terminal_outcome_logits': [[0, 0, math.log(2)]],
        'terminal_remaining': [math.log1p(3) / math.log1p(1500)]})
    assert result['discounted_value'] == 99
    assert result['probabilities'] == pytest.approx({'loss': 0.25, 'draw': 0.25, 'win': 0.5})
    assert result['remaining_events'] == pytest.approx(3)
    assert result['calibration'] == 'uncalibrated'
    assert serialize_model_evaluation([[0.8]])['probabilities'] is None
    assert serialize_model_evaluation([[float('nan')]])['discounted_value'] is None
    malformed = serialize_model_evaluation([[1]], {'terminal_outcome_logits': [[float('nan'), 0, 1]], 'terminal_remaining': [0]})
    assert malformed['probabilities'] is None and malformed['terminal_reason'] == 'invalid_terminal_output'


# 真实后端一次前向同时给动作和反馈，无候选场面也能从同一玩家可见信息评测
@pytest.mark.parametrize('backend', ['pytorch', 'onnxruntime'])
@pytest.mark.parametrize('player', [0, 1])
def test_actual_v4_feedback_reuses_forward_and_supports_passive_state(tmp_path, backend, player):
    assets = write_v4_assets(tmp_path / 'assets')
    path, _ = write_v4_checkpoint(tmp_path, assets) if backend == 'pytorch' else frozen_graph(tmp_path, assets)
    bot = AiBot(initialize_network=False)
    assert bot.load_model(path, asset_path=assets.root, protocol='v4')
    state, _ = make_state(player)
    if backend == 'pytorch':
        calls = []
        hook = bot.net.register_forward_hook(lambda *_: calls.append(1))
    decision = bot.get_scored_decision_from_snapshot(state.get_snapshot(), 11, collect_feedback=True)
    assert decision.feedback and np.isfinite(decision.feedback['discounted_value'])
    if backend == 'pytorch':
        assert len(calls) == 1
        hook.remove()
        assert sum(decision.feedback['probabilities'].values()) == pytest.approx(1)
    else:
        assert decision.feedback['probabilities'] is None
    report = deepcopy(bot.encoder.last_online_report)
    source = state.get_snapshot()
    source.valid_actions = []
    source.global_data.to_play = 1 - player
    result = bot.get_state_evaluation_from_snapshot(source, player)
    assert np.isfinite(result['discounted_value'])
    assert bot.encoder.last_online_report == report
    assert bot.encoder.last_state_evaluation_report['purpose'] == 'state_evaluation'
    with pytest.raises(ValueError, match='切换|接收者'):
        bot.get_state_evaluation_from_snapshot(source, 1 - player)
    assert not source.valid_actions


# 可选终局输出存在时直接读取，普通价值仍保持独立语义
def test_onnx_optional_terminal_outputs_are_used_without_reinterpreting_value(tmp_path):
    # 扩展受控图以验证可选输出合同，不修改或重导出任何真实用户模型
    onnx = pytest.importorskip('onnx')
    assets = write_v4_assets(tmp_path / 'assets')
    path, _ = frozen_graph(tmp_path, assets)
    graph = onnx.load(path)
    graph.graph.initializer.extend([
        onnx.numpy_helper.from_array(np.array([[0, 0, math.log(2)]], dtype=np.float32), 'test_terminal_logits'),
        onnx.numpy_helper.from_array(np.array([0.2], dtype=np.float32), 'test_terminal_remaining')])
    graph.graph.node.extend([
        onnx.helper.make_node('Identity', ['test_terminal_logits'], ['terminal_outcome_logits']),
        onnx.helper.make_node('Identity', ['test_terminal_remaining'], ['terminal_remaining'])])
    graph.graph.output.extend([
        onnx.helper.make_tensor_value_info('terminal_outcome_logits', onnx.TensorProto.FLOAT, [1, 3]),
        onnx.helper.make_tensor_value_info('terminal_remaining', onnx.TensorProto.FLOAT, [1])])
    onnx.save(graph, path)
    bot = AiBot(initialize_network=False)
    assert bot.load_model(path, asset_path=assets.root, protocol='v4')
    state, _ = make_state()
    decision = bot.get_scored_decision_from_snapshot(state.get_snapshot(), 11, collect_feedback=True)
    assert decision.feedback['probabilities']['win'] == pytest.approx(0.5)
    assert decision.feedback['remaining_events'] > 0


# 历史有界且对外副本隔离，重建代次可以使旧任务失效
def test_feedback_history_is_bounded_and_explicit_about_staleness():
    state, _ = make_state()
    history = ModelFeedbackHistory()
    for sequence in range(150):
        source = state.get_snapshot()
        source.online_coverage['sequence'] = sequence
        history.record(serialize_model_evaluation([[sequence]]), source, {}, source='decision_shared')
    report = history.snapshot(state_sequence=149, active=True)
    assert len(report['history']) == 128 and report['current']
    report['latest']['discounted_value'] = -999
    assert history.points[-1]['discounted_value'] == 149
    assert not history.snapshot(state_sequence=150, active=True)['current']
    epoch = history.epoch
    history.reset(clear=False)
    assert history.epoch == epoch + 1 and history.points


# 创建只拥有真实反馈调度与单线程模型池的隔离 Link
def feedback_link(state, worker):
    link = GalateaLink.__new__(GalateaLink)
    link.gamestate = state
    link.ai_player_id = 0
    link.duel_active = True
    link.model_feedback = ModelFeedbackHistory()
    link.decision_policy = InterventionPolicy(DecisionConfig(core_feedback_passive_enabled=True, core_time_budget=0.05))
    link.ai = SimpleNamespace(model_available=True, model_protocol_version=4, model_metadata={},
        get_state_evaluation_from_snapshot=worker)
    link._feedback_task = None
    link._feedback_future = None
    link._feedback_pending = None
    link.policy_executor = ThreadPoolExecutor(max_workers=1)
    link._publish_event = Mock()
    return link


# 超时后的模型线程不再向主操作排队，也不在退出后回写过期状态
@pytest.mark.asyncio
async def test_slow_passive_job_never_queues_main_decision_or_publishes_stale_result(monkeypatch):
    monkeypatch.setattr('galatea_link.FEEDBACK_SAMPLE_INTERVAL', 0.001)
    state, _ = make_state()
    entered, release = threading.Event(), threading.Event()
    # 在可控事件上等待以模拟无法被协程取消的模型线程
    def blocked(snapshot, player):
        entered.set()
        release.wait(2)
        return serialize_model_evaluation([[1]])
    link = feedback_link(state, blocked)
    try:
        link._schedule_feedback(state.get_snapshot(), 40)
        await asyncio.wait_for(link._feedback_task, 1)
        assert entered.is_set() and link._feedback_busy()
        assert await asyncio.wait_for(link._compute_core_decision(SimpleNamespace()), 0.1) is None
        link._cancel_feedback()
        release.set()
        await asyncio.wait_for(asyncio.shield(link._feedback_future), 1)
        assert not link._feedback_busy() and not link.model_feedback.points
        link._publish_event.assert_not_called()
    finally:
        release.set()
        link.policy_executor.shutdown(wait=True)


# 无动作评测只发布反馈事件，不会提交游戏动作，并拒绝更新前状态的迟到结果
@pytest.mark.asyncio
async def test_passive_job_publishes_only_current_feedback(monkeypatch):
    monkeypatch.setattr('galatea_link.FEEDBACK_SAMPLE_INTERVAL', 0.001)
    state, _ = make_state()
    link = feedback_link(state, lambda *_: serialize_model_evaluation([[0.25]]))
    try:
        link._schedule_feedback(state.get_snapshot(), 40)
        await asyncio.wait_for(link._feedback_task, 1)
        assert link.model_feedback.points[-1]['source'] == 'passive'
        assert link._publish_event.call_args.args[0] == 'model.feedback.updated'
        latest = link.get_model_feedback()
        assert latest['current'] and latest['latest']['perspective'] == 0
    finally:
        link.policy_executor.shutdown(wait=True)


# 主提示在复杂菜单准备前取消旁路，避免后台任务趁主干评分时进入模型队列
@pytest.mark.asyncio
async def test_main_prompt_cancels_passive_before_preparing_candidates():
    state, _ = make_state()
    link = feedback_link(state, lambda *_: serialize_model_evaluation([[0]]))
    pending = asyncio.create_task(asyncio.sleep(60))
    link._feedback_task = pending
    link._assign_ai_core_player = Mock()
    # 在主菜单准备入口检查取消已经发生，不等到最后提交动作时再处理
    async def prepare(*_):
        assert pending.cancelling() > 0
        return state.get_snapshot()
    link._prepare_prompt_snapshot = AsyncMock(side_effect=prepare)
    link._capture_llm_observation = Mock()
    link._schedule_decision = AsyncMock()
    try:
        await link.handle_server_msg(1, bytes([13]) + struct.pack('<BI', 0, 123))
        link._prepare_prompt_snapshot.assert_awaited_once()
        link._schedule_decision.assert_awaited_once()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        link.policy_executor.shutdown(wait=True)


# 更新前场面的迟到结果不能写入新的状态
@pytest.mark.asyncio
async def test_state_change_discards_inflight_passive_result(monkeypatch):
    # 推理已启动后推进状态，不因模型最终成功返回就接受旧场面的预测
    monkeypatch.setattr('galatea_link.FEEDBACK_SAMPLE_INTERVAL', 0.001)
    state, _ = make_state()
    entered, release = threading.Event(), threading.Event()
    # 用可控线程等待复现服务器在模型返回前继续更新场面
    def blocked(snapshot, player):
        entered.set()
        release.wait(1)
        return serialize_model_evaluation([[1]])
    link = feedback_link(state, blocked)
    try:
        link._schedule_feedback(state.get_snapshot(), 40)
        await asyncio.to_thread(entered.wait, 0.5)
        state.update(40, b'\1')
        release.set()
        await asyncio.wait_for(link._feedback_task, 1)
        assert not link.model_feedback.points
        link._publish_event.assert_not_called()
    finally:
        release.set()
        link.policy_executor.shutdown(wait=True)


# 反馈接口沿用鉴权且空闲读取不加载模型，离线开关通过统一接口持久修改
@pytest.mark.asyncio
async def test_feedback_http_is_authenticated_lazy_and_offline_controls_are_supported(tmp_path):
    (tmp_path / 'config.yaml').write_text('{}\n', encoding='utf-8')
    manager = LinkSessionManager(tmp_path / 'config.yaml')
    manager._runtime_builder = Mock()
    client = TestClient(TestServer(create_http_app(manager, ServiceConfig(api_token='test-token'))))
    await client.start_server()
    try:
        assert (await client.get('/api/v1/model-feedback')).status == 401
        headers = {'Authorization': 'Bearer test-token'}
        response = await client.get('/api/v1/model-feedback', headers=headers)
        assert response.status == 200 and not (await response.json())['data']['available']
        controls = (await (await client.get('/api/v1/controls', headers=headers)).json())['data']
        response = await client.patch('/api/v1/controls', headers=headers, json={
            'expected_revision': controls['revision'], 'patch': {'intervention': {
                'core_feedback_enabled': False, 'core_feedback_passive_enabled': True}}})
        assert response.status == 200
        payload = (await response.json())['data']['intervention']
        assert payload['core_feedback_enabled'] is False and payload['core_feedback_passive_enabled'] is True
        assert manager._link is None
        manager._runtime_builder.assert_not_called()
        cached = (await (await client.get('/api/v1/model-feedback', headers=headers)).json())['data']
        assert not cached['enabled'] and cached['passive_enabled']
        restored = LinkSessionManager(tmp_path / 'config.yaml')
        assert not restored.get_controls()['intervention']['core_feedback_enabled']
    finally:
        await client.close()
