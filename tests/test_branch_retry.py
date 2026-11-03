# 末端优先回退回归，覆盖真实闭环普通多选硬拒绝和异步失败隔离

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from agents.ai_bot import AiBot, CoreDecision
from agents.decision_policy import InterventionPolicy
from app_config import DecisionConfig
from core.branch_retry import CoreBranchRetry, action_token
from data_types import GameAction
from tests.test_response_history import commit_link


# 构造只有决策候选变化而实际局面保持相同的提示
def snapshot(actions):
    return SimpleNamespace(global_data=SimpleNamespace(to_play=0, turn_count=1, phase_id=4, lp=8000),
        entities=[], chain_stack=[], history_stack=[], valid_actions=deepcopy(actions))


MAIN = [GameAction(5, 0, operation_id=15), GameAction(6, 0, operation_id=17)]
LEAF = [GameAction(13, 0, operation_id=2), GameAction(13, 1, operation_id=1)]


# 走一次进入选择并取消回主菜单的闭环，返回当前主菜单和实际调整
def cycle(policy, main, leaf_choice=0):
    policy.stage(main, 0)
    leaf = snapshot(LEAF)
    policy.enter(leaf, 13, b"leaf")
    policy.stage(leaf, leaf_choice)
    returned = snapshot(MAIN)
    changes, _ = policy.enter(returned, 11, b"main")
    return returned, changes


# 第五次闭合回退先改末端而不直接放弃主入口，末端耗尽才向上调整
def test_five_cycles_prefer_leaf_then_propagate_to_parent():
    policy = CoreBranchRetry()
    main = snapshot(MAIN)
    policy.enter(main, 11, b"main")
    for repeat in range(5):
        main, changes = cycle(policy, main)
        assert bool(changes) == (repeat == 4)
    assert [item["message_type"] for item in changes] == [13]
    assert main.core_soft_banned_choice_ids == ()
    for _ in range(5):
        main, changes = cycle(policy, main, leaf_choice=1)
    assert [item["message_type"] for item in changes] == [13, 11]
    assert main.core_soft_banned_choice_ids == (0,)


# 连续同类多选没有回退证据时保持原策略，真实局面变化清理旧软名单
def test_progressing_selection_and_world_change_do_not_false_trigger():
    policy = CoreBranchRetry(threshold=1)
    for _ in range(8):
        prompt = snapshot([GameAction(15, 0, operation_id=4), GameAction(15, 1, operation_id=4)])
        changes, _ = policy.enter(prompt, 15, b"select")
        assert not changes and not prompt.core_soft_banned_choice_ids
        policy.stage(prompt, 0)
    main = snapshot(MAIN)
    policy.enter(main, 11, b"main")
    main, _ = cycle(policy, main)
    main.global_data.lp = 7000
    assert policy.enter(main, 11, b"main")[0] == []
    assert not main.core_soft_banned_choice_ids


# 服务器硬拒绝不消耗闭环次数，失败写出和旧局回调不能遗留软路径
def test_engine_retry_and_failed_send_do_not_count_as_cycles():
    policy = CoreBranchRetry(threshold=1)
    prompt = snapshot(MAIN)
    policy.enter(prompt, 11, b"main")
    policy.stage(prompt, 0)
    changes, _ = policy.enter(prompt, 11, b"main", engine_retry=True, hard_bans=(0,))
    assert not changes and not prompt.core_soft_banned_choice_ids
    assert policy._paths[0] == []
    token = policy.stage(prompt, 1)
    policy.abort(token)
    assert not policy._paths
    policy.enter(prompt, 11, b"main")
    new = policy.stage(prompt, 0)
    policy.abort(token)
    assert policy._paths[0] and new != token


# 宏池重排不改变显式响应身份，旧查询序号也不伪装成局面推进
def test_macro_identity_and_query_sequence_are_stable():
    one = GameAction(15, 0, operation_id=20, decision_bytes=b"\1\2")
    two = deepcopy(one)
    two.index, two.desc_str = 9, "不同显示文本"
    assert action_token(one) == action_token(two)
    policy = CoreBranchRetry()
    prompt = snapshot(MAIN)
    prompt.entities = [SimpleNamespace(code=14558127, query_sequence=1, relation_known_fields=())]
    previous = policy.world_key(prompt)
    prompt.entities[0].query_sequence = 8
    prompt.entities[0].relation_known_fields = ("target_locations",)
    assert previous == policy.world_key(prompt)


# 策略掩码在贪心和温度采样均生效，软名单耗尽也不恢复硬拒绝
@pytest.mark.parametrize("mode", ["greedy", "deployment"])
def test_model_selection_preserves_hard_bans_when_soft_choices_exhausted(mode):
    bot = AiBot.__new__(AiBot)
    bot.net = SimpleNamespace(eval=Mock())
    bot.encoder = SimpleNamespace(encode=Mock(return_value={}))
    bot._infer = Mock(return_value=(np.asarray([[20., 10.]]), None, None))
    prompt = snapshot(LEAF)
    prompt.entities = [object()]
    decision = bot.get_scored_decision_from_snapshot(prompt, 13, policy_mode=mode,
        excluded_choice_ids=(0,), soft_excluded_choice_ids=(1,))
    assert decision.choice_id == 1 and decision.available_choice_ids == (1,)
    assert bot.get_scored_decision_from_snapshot(prompt, 13, excluded_choice_ids=(0, 1)) is None


# 发送期间已到下一提示时暂存路径可见，失败返回则丢弃所有同代软证据
@pytest.mark.asyncio
async def test_link_stages_before_network_wait_and_discards_failure():
    link, request, outcome = commit_link(None)
    link.decision_policy = SimpleNamespace(config=DecisionConfig())
    link._branch_retry = CoreBranchRetry()
    link._branch_retry.enter(request.snapshot, 13, request.raw_msg[1:])
    # 模拟网络确认前游戏任务已经推进到了下一提示
    async def send(response):
        assert link._branch_retry._paths[0]
        return False
    link.client.send_decision = send
    with pytest.raises(ConnectionError):
        await link._commit_decision(request, outcome)
    assert not link._branch_retry._paths
    link._publish_event.assert_not_called()


# 软避让不裁剪 LLM 菜单，服务器硬拒绝则连同相同响应的候选一起排除
@pytest.mark.asyncio
async def test_remote_menu_preserves_soft_choices_and_filters_wire_aliases():
    link, request, _ = commit_link(AsyncMock(return_value=True))
    link.decision_policy = SimpleNamespace(config=DecisionConfig())
    link.ignore_actions_blacklist = [0]
    link.ignore_choice_ids_blacklist = [0]
    request.snapshot.valid_actions = [GameAction(13, 0), GameAction(13, 0), GameAction(13, 1)]
    link.latest_observation = {"legal_actions": [{"choice_id": index} for index in range(3)]}
    link.decision_coordinator = SimpleNamespace(submit=AsyncMock(return_value=SimpleNamespace(request_id=9)))
    link._branch_retry = CoreBranchRetry()
    # 模拟软策略希望避开第三个候选，远程智能体仍可自主选择它
    def enter(snap, *args, **kwargs):
        snap.core_soft_banned_choice_ids = (2,)
        return [], False
    link._branch_retry.enter = enter
    await link._schedule_decision(13, request.raw_msg, request.snapshot)
    submitted = link.decision_coordinator.submit.call_args.kwargs["observation"]
    assert [action["choice_id"] for action in submitted["legal_actions"]] == [2]
    assert request.snapshot.core_hard_banned_choice_ids == (0, 1)


# 存在服务器拒绝记录时仍让 Core 在其余动作中重选，不直接跳过模型
@pytest.mark.asyncio
async def test_server_retry_runs_core_with_remaining_candidates():
    link, request, _ = commit_link(AsyncMock(return_value=True))
    link.decision_policy = InterventionPolicy(DecisionConfig(mode="core_only"))
    request = replace(request, ignore_actions=(0,))
    request.snapshot.core_hard_banned_choice_ids = (0,)
    decision = CoreDecision(1, request.snapshot.valid_actions[1], 1, 1.0, 1.0)
    scored = Mock(return_value=decision)
    link.ai = SimpleNamespace(model_available=True, get_scored_decision_from_snapshot=scored)
    link._compute_rule_decision = AsyncMock()
    outcome = await link._compute_decision(request)
    assert outcome.source == "core" and outcome.choice_id == 1
    assert scored.call_args.kwargs["excluded_choice_ids"] == (0,)
    link._compute_rule_decision.assert_not_awaited()
