# Galatea 混合决策测试，验证 LLM 动作编号能转换并提交为游戏响应

import unittest
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.ai_bot import CoreDecision
from agents.decision_policy import DecisionOutcome, InterventionPolicy
from agents.llm_client import LlmDecision, LlmInterventionUpdate
from app_config import DecisionConfig
from core.decision_runtime import DecisionRequest
from core.link_events import LinkEventBus
from core.runtime_api import GalateaRuntimeApi
from core.remote_decisions import RemoteDecisionBroker
from galatea_link import GalateaLink


class FakeLlmClient:
    # 返回固定的第二个合法动作
    async def decide(self, observation):
        return LlmDecision(choice_id=1, reason="测试选择", chat_message="你好")


class SlowLlmClient:
    # 等待取消以模拟没有及时返回的远端接口
    async def decide(self, observation):
        await asyncio.Event().wait()


class SupervisingLlmClient:
    # 初始化用于检查运行时控制注入的观察记录
    def __init__(self):
        self.observations = []

    # 返回引用当前控制版本的动作和宏观调整
    async def decide(self, observation):
        self.observations.append(observation)
        return LlmDecision(
            choice_id=1,
            reason="测试监督调整",
            intervention_update=LlmInterventionUpdate(
                mode="hybrid",
                ttl_decisions=2,
                reason="后续根据置信度介入",
                base_revision=observation["runtime_controls"]["revision"],
            ),
        )


class FakeAi:
    # 将动作编号转换成便于断言的响应
    def pack_choice_from_snapshot(self, snapshot, choice_id, msg_type):
        return f"packed:{msg_type}:{choice_id}"


class FakeConstructedAi:
    # 模拟 GalateaLink 初始化期间使用的 Core 模型包装器
    def __init__(self, *args, **kwargs):
        self.env = None
        self.model_protocol_version = 3

    # 跳过真实模型加载并保持协议版本
    def load_model(self, *args, **kwargs):
        return False


class FakeOnnxAi:
    # 初始化只有统一推理运行时可用而没有 PyTorch 网络的测试模型
    def __init__(self):
        self.model_available = True
        self.net = None
        self.calls = []

    # 返回固定 Core 决策并记录温度参数
    def get_scored_decision_from_snapshot(
        self,
        snapshot,
        msg_type,
        *,
        policy_mode,
        temperature,
    ):
        self.calls.append((snapshot, msg_type, policy_mode, temperature))
        return CoreDecision(
            choice_id=0,
            action=None,
            response=b"onnx-response",
            confidence=0.75,
            probability_margin=0.5,
            policy_mode=policy_mode,
            temperature=temperature,
        )


class FakeClient:
    # 初始化已发送决策记录
    def __init__(self):
        self.sent_decisions = []

    # 保存提交给游戏网络层的决策
    async def send_decision(self, response):
        self.sent_decisions.append(response)


class GalateaDecisionTests(unittest.IsolatedAsyncioTestCase):
    # 验证字符串配置的资产路径在启动构造时转换为 Path
    async def test_constructor_normalizes_model_asset_path(self):
        with tempfile.TemporaryDirectory() as directory:
            asset_root = Path(directory) / "model_assets" / "v3"
            asset_root.mkdir(parents=True)
            deck = SimpleNamespace(name="测试卡组", main=[], extra=[])
            with (
                patch("galatea_link.AiBot", FakeConstructedAi),
                patch("galatea_link.load_deck", return_value=deck),
                patch("galatea_link.card_db.reload"),
            ):
                link = GalateaLink(
                    "127.0.0.1",
                    7911,
                    "",
                    "测试卡组",
                    model_assets_path=str(asset_root),
                )

            self.assertIsInstance(link.model_assets_path, Path)
            self.assertEqual(link.model_assets_path, asset_root.resolve())
            await link.close()

    # 验证远程 AstrBot 决策会收到可见观察并可提交合法动作
    async def test_remote_astrbot_decision_round_trip(self):
        link = GalateaLink.__new__(GalateaLink)
        link.decision_policy = InterventionPolicy(
            DecisionConfig(
                mode="llm_only",
                agent_backend="remote_astrbot",
                llm_time_budget=0.5,
            )
        )
        link.llm_client = None
        link.ai = FakeAi()
        link.event_bus = LinkEventBus()
        link.remote_decisions = RemoteDecisionBroker(link.event_bus)
        snapshot = SimpleNamespace(valid_actions=[object(), object()])
        request = DecisionRequest(
            request_id=11,
            msg_type=13,
            raw_msg=b"",
            snapshot=snapshot,
            observation={
                "observation_id": 17,
                "decision_required": True,
                "legal_actions": [{"choice_id": 0}, {"choice_id": 1}],
            },
        )

        task = asyncio.create_task(link._compute_decision(request))
        while link.remote_decisions.get_pending() is None:
            await asyncio.sleep(0)
        pending = link.remote_decisions.get_pending()
        link.remote_decisions.submit(
            11,
            {
                "session_id": pending["session_id"],
                "observation_id": pending["observation_id"],
                "choice_id": 1,
                "reason": "由 AstrBot 主智能体选择",
            },
        )
        outcome = await task

        self.assertEqual(outcome.source, "astrbot")
        self.assertEqual(outcome.choice_id, 1)
        self.assertEqual(outcome.response, "packed:13:1")
        link.event_bus.close()

    # 验证没有 PyTorch net 的 ONNX 后端仍会执行 Core 推理
    async def test_core_decision_accepts_onnx_only_backend(self):
        link = GalateaLink.__new__(GalateaLink)
        link.ai = FakeOnnxAi()
        link.policy_executor = None
        link.decision_policy = InterventionPolicy(
            DecisionConfig(
                mode="core_only",
                core_policy_mode="deployment",
                core_temperature=1.1,
            )
        )
        snapshot = SimpleNamespace(valid_actions=[object()])
        request = DecisionRequest(
            request_id=0,
            msg_type=13,
            raw_msg=b"",
            snapshot=snapshot,
        )

        result = await link._compute_core_decision(request)

        self.assertEqual(result.response, b"onnx-response")
        self.assertEqual(
            link.ai.calls,
            [(snapshot, 13, "deployment", 1.1)],
        )

    # 验证宏动作候选失败时不会继续调用 Core 或 LLM
    async def test_macro_failure_forces_rule_fallback(self):
        link = GalateaLink.__new__(GalateaLink)

        async def compute_rule(request):
            return b"rule-macro"

        link._compute_rule_decision = compute_rule
        request = DecisionRequest(
            request_id=0,
            msg_type=15,
            raw_msg=b"",
            snapshot=SimpleNamespace(force_rule_fallback=True),
        )

        outcome = await link._compute_decision(request)

        self.assertEqual(outcome.source, "rule")
        self.assertEqual(outcome.response, b"rule-macro")

    # 验证 llm_only 路径完全跳过 Core 并保留聊天建议
    async def test_llm_only_translates_choice_to_response(self):
        link = GalateaLink.__new__(GalateaLink)
        link.decision_policy = InterventionPolicy(DecisionConfig(mode="llm_only"))
        link.llm_client = FakeLlmClient()
        link.ai = FakeAi()
        link.event_bus = LinkEventBus()
        subscription = link.event_bus.subscribe()
        snapshot = SimpleNamespace(valid_actions=[object(), object()])
        request = DecisionRequest(
            request_id=1,
            msg_type=13,
            raw_msg=b"",
            snapshot=snapshot,
            observation={
                "decision_required": True,
                "legal_actions": [{"choice_id": 0}, {"choice_id": 1}],
            },
        )

        outcome = await link._compute_decision(request)

        self.assertEqual(outcome.source, "llm")
        self.assertEqual(outcome.choice_id, 1)
        self.assertEqual(outcome.response, "packed:13:1")
        self.assertEqual(outcome.chat_message, "你好")
        requested_event = await subscription.get()
        completed_event = await subscription.get()
        self.assertEqual(requested_event.event_type, "llm.requested")
        self.assertEqual(completed_event.event_type, "llm.completed")
        link.event_bus.close()

    # 验证 LLM 超过独立预算后会取消并进入规则兜底
    async def test_llm_time_budget_falls_back_to_rule(self):
        link = GalateaLink.__new__(GalateaLink)
        link.decision_policy = InterventionPolicy(
            DecisionConfig(mode="llm_only", llm_time_budget=0.01)
        )
        link.llm_client = SlowLlmClient()
        link.ai = FakeAi()

        # 返回固定结果以模拟规则兜底
        async def compute_rule(request):
            return "rule-response"

        link._compute_rule_decision = compute_rule
        snapshot = SimpleNamespace(valid_actions=[object()])
        request = DecisionRequest(
            request_id=2,
            msg_type=13,
            raw_msg=b"",
            snapshot=snapshot,
            observation={
                "decision_required": True,
                "legal_actions": [{"choice_id": 0}],
            },
        )

        outcome = await link._compute_decision(request)

        self.assertEqual(outcome.source, "rule")
        self.assertEqual(outcome.response, "rule-response")

    # 验证最终动作和聊天建议会进入外部事件流
    async def test_commit_publishes_decision_and_chat_events(self):
        link = GalateaLink.__new__(GalateaLink)
        link.client = FakeClient()
        link.event_bus = LinkEventBus()
        link.last_decision = None
        link.last_decision_choice_id = None
        link.last_decision_source = None
        link.latest_chat_suggestion = None
        subscription = link.event_bus.subscribe()
        request = DecisionRequest(
            request_id=3,
            msg_type=13,
            raw_msg=b"",
            snapshot=SimpleNamespace(valid_actions=[]),
        )
        outcome = DecisionOutcome(
            response=b"\x01",
            source="llm",
            choice_id=1,
            reason="测试提交",
            chat_message="祝你好运",
            core_confidence=0.4,
        )

        await link._commit_decision(request, outcome)
        decision_event = await subscription.get()
        chat_event = await subscription.get()

        self.assertEqual(link.client.sent_decisions, [b"\x01"])
        self.assertEqual(decision_event.event_type, "decision.committed")
        self.assertEqual(decision_event.payload["source"], "llm")
        self.assertEqual(chat_event.event_type, "chat.suggested")
        self.assertEqual(chat_event.payload["message"], "祝你好运")
        link.event_bus.close()

    # 验证 LLM 能读取统一控制状态并在提交后临时调整后续策略
    async def test_supervisor_update_applies_after_current_decision(self):
        link = GalateaLink.__new__(GalateaLink)
        link.decision_policy = InterventionPolicy(
            DecisionConfig(
                mode="llm_only",
                autonomous_intervention_enabled=True,
            )
        )
        link.event_bus = LinkEventBus()
        link.runtime = GalateaRuntimeApi(link, link.event_bus)
        link.llm_client = SupervisingLlmClient()
        link.ai = FakeAi()
        link.client = FakeClient()
        link.last_decision = None
        link.last_decision_choice_id = None
        link.last_decision_source = None
        link.latest_chat_suggestion = None
        snapshot = SimpleNamespace(valid_actions=[object(), object()])
        request = DecisionRequest(
            request_id=4,
            msg_type=13,
            raw_msg=b"",
            snapshot=snapshot,
            observation={
                "decision_required": True,
                "legal_actions": [{"choice_id": 0}, {"choice_id": 1}],
            },
        )

        outcome = await link._compute_decision(request)

        self.assertEqual(link.decision_policy.config.mode, "llm_only")
        self.assertTrue(
            link.llm_client.observations[0]["runtime_controls"]["autonomy"][
                "enabled"
            ]
        )
        await link._commit_decision(request, outcome)
        self.assertEqual(link.decision_policy.config.mode, "hybrid")
        self.assertEqual(
            link.runtime.get_controls()["autonomy"]["active_override"][
                "remaining_decisions"
            ],
            2,
        )
        link.event_bus.close()


if __name__ == "__main__":
    unittest.main()
