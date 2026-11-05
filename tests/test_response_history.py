# 本方响应证据回归，覆盖网络失败取消重置异步到达与真实历史边界

import asyncio
from copy import deepcopy
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.ai_bot import AiBot
from agents.decision_policy import DecisionOutcome
from core.decision_runtime import DecisionRequest
from core.gamestate import DuelState
from core.network import YgoNetClient
from core.response_history import OnlineResponseHistory, VisibleStateDigest, MAX_CAPTURE_MESSAGES
from data_types import GameAction
from galatea_link import GalateaLink
from model_protocols.v4.data_types import TransitionResultFlag
from model_protocols.v4.online_adapter import project_own_history
from tests.test_online_state import start_payload


DIGEST = VisibleStateDigest(8000, 8000, (0,) * 16, 0)


# 建立仅属于玩家零的待发送草稿
def prepare(history, action=None):
    history.bind(0)
    return history.prepare(request_id=1, actor=0, prompt_type=13, turn=1, phase=4,
        action=action or GameAction(13, 0, operation_id=2), choice_id=0, digest=DIGEST)


# 网络等待期间到达的事件有序保留，未确认草稿对快照不可见
def test_draft_is_invisible_until_send_and_boundary_are_confirmed():
    history = OnlineResponseHistory()
    token = prepare(history)
    after = VisibleStateDigest(7000, 8000, (0,) * 16, 0)
    history.observe(91, struct.pack("<BI", 0, 1000), after)
    history.observe(13, b"", after, interaction=True)
    assert history.snapshot() == ()
    history.confirm(token)
    record = history.snapshot()[0]
    assert record.messages[0][0] == 91 and record.after.lp_p0 == 7000
    assert history.snapshot() == (record,)
    events, skipped = project_own_history(history.snapshot(), 0)
    assert skipped == 0 and events[0].lp_delta_p0 == -1000
    assert events[0].event_visibility_mask == 1
    assert events[0].result_flags & int(TransitionResultFlag.DECLINED)
    assert history.coverage()["model_history_complete"] is False


# 原动作列表的修改不能改变已经发出的动作语义
def test_action_evidence_does_not_reference_mutable_candidate_lists():
    action = GameAction(15, 0, macro_targets=[0], macro_target_codes=[14558127])
    history = OnlineResponseHistory()
    token = prepare(history, action)
    history.confirm(token)
    action.macro_target_codes[0] = 89631139
    history.observe(13, b"", DIGEST, interaction=True)
    assert dict(history.snapshot()[0].action)["macro_target_codes"] == (14558127,)


# 失败或新对局到达后旧确认不生成记录，也不能删除新草稿
def test_failed_and_stale_tokens_cannot_pollute_next_duel():
    history = OnlineResponseHistory()
    old = prepare(history)
    history.abort(old)
    assert history.snapshot() == ()
    history.reset()
    new = prepare(history)
    assert history.confirm(old) is False
    history.abort(old)
    assert history.coverage()["pending"] is True
    history.confirm(new)
    history.observe(1, b"", DIGEST)
    events, _ = project_own_history(history.snapshot(), 0)
    assert events[0].result_flags & int(TransitionResultFlag.RETRY)
    assert not events[0].result_flags & int(TransitionResultFlag.RESOLVED)


# 不完整采集不伪装成完整事件，重建只计一次来源缺口
def test_capture_overflow_and_rebuild_are_reported():
    history = OnlineResponseHistory()
    token = prepare(history)
    history.confirm(token)
    for _ in range(MAX_CAPTURE_MESSAGES + 1):
        history.observe(91, struct.pack("<BI", 0, 0), DIGEST)
    history.observe(13, b"", DIGEST, interaction=True)
    assert project_own_history(history.snapshot(), 0) == ([], 1)
    prepare(history)
    history.invalidate()
    assert history.coverage()["gaps"]["state_rebuild"] == 1
    assert history.snapshot() == ()


# 查询和空连锁都不会把一次已提交响应切成额外模型决策
def test_queries_and_empty_chain_do_not_create_boundaries():
    state = DuelState()
    state.update(4, start_payload())
    token = prepare(state.response_history)
    state.response_history.confirm(token)
    state.update(16, struct.pack("<BBBII", 0, 0, 0, 0, 0))
    assert state.response_history.snapshot() == ()
    state.get_snapshot()
    state.get_snapshot()
    assert state.response_history.snapshot() == ()
    state.update(13, struct.pack("<BI", 0, 0))
    assert len(state.response_history.snapshot()) == 1


# 建立可测试实际发送分支的 Link，不连接任何外部服务器
def commit_link(send):
    link = GalateaLink.__new__(GalateaLink)
    link.gamestate = DuelState()
    link.gamestate.update(4, start_payload())
    link.gamestate.update(13, struct.pack("<BI", 0, 0))
    link.ai = AiBot(initialize_network=False)
    link.client = SimpleNamespace(send_decision=send)
    link._publish_event = Mock()
    link._apply_decision_controls = AsyncMock()
    request = DecisionRequest(1, 13, bytes([13]) + struct.pack("<BI", 0, 0), link.gamestate.get_snapshot())
    outcome = DecisionOutcome(response=0, source="llm", choice_id=1)
    return link, request, outcome


# 写出失败不能发布成功提交或保留虚假历史
@pytest.mark.asyncio
async def test_failed_send_is_not_committed():
    link, request, outcome = commit_link(AsyncMock(return_value=False))
    with pytest.raises(ConnectionError, match="未能写入"):
        await link._commit_decision(request, outcome)
    assert link.gamestate.response_history.snapshot() == ()
    assert not link.gamestate.response_history.coverage()["pending"]
    link._publish_event.assert_not_called()


# 发送期间收到下一提示不会漏掉前一响应的实际区间
@pytest.mark.asyncio
async def test_prompt_arriving_during_send_is_preserved():
    link, request, outcome = commit_link(None)
    # 模拟 drain 等待中服务器已经返回公开事件和下一提示
    async def send(response):
        link.gamestate.update(91, struct.pack("<BI", 0, 800))
        link.gamestate.update(13, struct.pack("<BI", 0, 0))
        assert link.gamestate.response_history.snapshot() == ()
        return True
    link.client.send_decision = send
    await link._commit_decision(request, outcome)
    record = link.gamestate.response_history.snapshot()[0]
    assert record.after.lp_p0 == 6200
    assert record.boundary == "next_own_prompt"
    link._publish_event.assert_called_once()


# 取消网络等待时丢弃草稿，不在后台补记旧动作
@pytest.mark.asyncio
async def test_cancelled_send_discards_draft():
    started = asyncio.Event()
    # 模拟可被正常取消的网络等待
    async def send(response):
        started.set()
        await asyncio.Event().wait()
    link, request, outcome = commit_link(send)
    task = asyncio.create_task(link._commit_decision(request, outcome))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not link.gamestate.response_history.coverage()["pending"]
    link._publish_event.assert_not_called()


# 对局重置后旧发送即使返回也不能应用策略或发布本局提交
@pytest.mark.asyncio
async def test_reset_during_send_ignores_late_confirmation():
    link, request, outcome = commit_link(None)
    # 模拟另一异步任务重置了对局
    async def send(response):
        link.gamestate.reset()
        return True
    link.client.send_decision = send
    await link._commit_decision(request, outcome)
    link._apply_decision_controls.assert_not_awaited()
    link._publish_event.assert_not_called()


# 网络层如实返回断开和 drain 失败，避免上层误报发送成功
@pytest.mark.asyncio
async def test_network_send_returns_transport_failure():
    client = YgoNetClient("127.0.0.1", 7911, "", None)
    assert await client.send_decision(0) is False
    client.is_connected = True
    client.writer = SimpleNamespace(write=Mock(), drain=AsyncMock(side_effect=ConnectionError("test")))
    assert await client.send_decision(0) is False


# 提示保留挂起菜单，动作池暂缺时 Retry 仍恢复原始宏动作坐标
@pytest.mark.asyncio
async def test_retry_restores_original_prompt_actions_after_hint():
    link, request, outcome = commit_link(AsyncMock(return_value=True))
    raw_location = 2 << 8
    action = GameAction(15, 0, target_entity_idx=raw_location, macro_targets=[raw_location], decision_bytes=b"\1\0")
    link.last_prompt_actions = deepcopy([action])
    link.last_prompt_type = 15
    link.last_prompt_msg = b"prompt"
    link.last_decision = 0
    link.last_decision_choice_id = 0
    link.ignore_actions_blacklist = []
    link.ignore_choice_ids_blacklist = []
    link._schedule_decision = AsyncMock()
    pending_actions = list(link.gamestate.current_valid_actions)
    link.gamestate.update(160, struct.pack("<IBI", 0, 0, 0))
    assert link.gamestate.current_valid_actions == pending_actions
    # 单独模拟上下文暂缺动作池，继续验证 Retry 的恢复路径
    link.gamestate.current_valid_actions = []
    await link.handle_server_msg(1, bytes([1]))
    link._schedule_decision.assert_awaited_once_with(15, b"prompt", engine_retry=True)
    assert link.gamestate.current_valid_actions[0].macro_targets == [raw_location]
    assert link.gamestate.current_valid_actions[0].decision_bytes == b"\1\0"
