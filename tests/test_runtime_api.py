# 外部运行时接口测试，验证状态读取、介入更新和平台消息投递

import unittest
from types import SimpleNamespace

from agents.decision_policy import InterventionPolicy
from agents.llm_client import LlmInterventionUpdate
from app_config import DecisionConfig
from core.link_events import LinkEventBus
from core.runtime_api import GalateaRuntimeApi


class FakeLink:
    # 初始化外部接口测试所需的最小对局状态
    def __init__(self):
        self.client = SimpleNamespace(is_connected=True)
        self.duel_active = True
        self.ai_player_id = 1
        self.is_room_host = True
        self.room_duel_mode = 0
        self.room_ready = {0: True, 1: True}
        self._start_requested = True
        self.last_duel_result = {"outcome": "self_win", "reason": "投降"}
        self.decision_policy = InterventionPolicy(
            DecisionConfig(mode="hybrid", core_confidence_threshold=0.65)
        )
        self.decision_coordinator = SimpleNamespace(active_request_id=7)
        self.last_decision_source = "core"
        self.last_decision_choice_id = 2
        self.latest_chat_suggestion = "你好"
        self.latest_observation = {"observation_id": 11, "cards": []}

    # 返回最新观察的独立副本
    def get_latest_llm_observation(self):
        return {
            "observation_id": self.latest_observation["observation_id"],
            "cards": list(self.latest_observation["cards"]),
        }


class RuntimeApiTests(unittest.IsolatedAsyncioTestCase):
    # 验证状态接口不暴露配置密钥并返回决策摘要
    async def test_returns_runtime_status(self):
        bus = LinkEventBus()
        api = GalateaRuntimeApi(FakeLink(), bus)

        status = api.get_status()

        self.assertTrue(status["connected"])
        self.assertTrue(status["duel_active"])
        self.assertFalse(status["core_model"]["available"])
        self.assertEqual(status["decision"]["active_request_id"], 7)
        self.assertEqual(status["decision"]["core_time_budget"], 5.0)
        self.assertEqual(status["decision"]["llm_time_budget"], 12.0)
        self.assertTrue(status["lobby"]["is_host"])
        self.assertEqual(status["lobby"]["ready_players"], [0, 1])
        self.assertEqual(status["last_duel_result"]["outcome"], "self_win")
        self.assertEqual(status["latest_observation_id"], 11)
        self.assertNotIn("api_key", repr(status))
        bus.close()

    # 验证运行中介入设置会原子更新并产生事件
    async def test_updates_intervention_and_publishes_event(self):
        link = FakeLink()
        bus = LinkEventBus()
        api = GalateaRuntimeApi(link, bus)
        subscription = api.subscribe_events()

        result = await api.update_intervention(
            mode="llm_only",
            core_confidence_threshold=0.4,
            force_llm_message_types=[16, 13, 16],
        )
        event = await subscription.get()

        self.assertEqual(link.decision_policy.config.mode, "llm_only")
        self.assertEqual(result["force_llm_message_types"], [16, 13])
        self.assertEqual(event.event_type, "runtime.controls.updated")
        bus.close()

    # 验证统一控制面可以同时更新人工基线和自主介入开关
    async def test_updates_unified_controls_with_revision(self):
        link = FakeLink()
        bus = LinkEventBus()
        api = GalateaRuntimeApi(link, bus)
        subscription = api.subscribe_events()

        controls = await api.update_controls(
            {
                "intervention": {
                    "mode": "llm_review",
                    "core_time_budget": 4.0,
                    "llm_time_budget": 9.0,
                    "include_core_suggestion": False,
                },
                "autonomy": {
                    "enabled": True,
                    "allowed_modes": ["hybrid", "core_only"],
                    "max_ttl_decisions": 2,
                },
            },
            source="astrbot.qq",
            expected_revision=0,
        )
        event = await subscription.get()

        self.assertEqual(controls["revision"], 1)
        self.assertEqual(controls["intervention"]["mode"], "llm_review")
        self.assertEqual(controls["intervention"]["core_time_budget"], 4.0)
        self.assertTrue(controls["autonomy"]["enabled"])
        self.assertEqual(event.payload["source"], "astrbot.qq")
        with self.assertRaisesRegex(RuntimeError, "版本冲突"):
            await api.update_controls(
                {"intervention": {"mode": "hybrid"}},
                expected_revision=0,
            )
        bus.close()

    # 验证自主调整按 TTL 生效并自动恢复人工基线
    async def test_autonomous_override_expires_to_baseline(self):
        link = FakeLink()
        link.decision_policy.config = DecisionConfig(
            mode="hybrid",
            autonomous_intervention_enabled=True,
            autonomous_allowed_modes=("hybrid", "core_only"),
            autonomous_max_ttl_decisions=2,
        )
        bus = LinkEventBus()
        api = GalateaRuntimeApi(link, bus)
        update = LlmInterventionUpdate(
            mode="core_only",
            core_confidence_threshold=0.4,
            ttl_decisions=5,
            reason="连续高置信度时暂时交给 Core",
            base_revision=0,
        )

        applied = await api.apply_autonomous_intervention(update, request_id=8)

        self.assertTrue(applied)
        self.assertEqual(link.decision_policy.config.mode, "core_only")
        self.assertEqual(
            api.get_controls()["autonomy"]["active_override"][
                "remaining_decisions"
            ],
            2,
        )
        await api.on_decision_committed(9)
        self.assertEqual(link.decision_policy.config.mode, "core_only")
        refreshed = await api.apply_autonomous_intervention(
            LlmInterventionUpdate(
                mode="hybrid",
                ttl_decisions=2,
                base_revision=1,
            ),
            request_id=10,
        )
        self.assertFalse(refreshed)
        await api.on_decision_committed(10)
        self.assertEqual(link.decision_policy.config.mode, "hybrid")
        self.assertIsNone(
            api.get_controls()["autonomy"]["active_override"]
        )
        bus.close()

    # 验证关闭自主开关时模型建议只会产生拒绝事件
    async def test_autonomous_update_is_ignored_when_disabled(self):
        link = FakeLink()
        bus = LinkEventBus()
        api = GalateaRuntimeApi(link, bus)
        subscription = api.subscribe_events()

        applied = await api.apply_autonomous_intervention(
            LlmInterventionUpdate(mode="llm_only", ttl_decisions=1),
            request_id=12,
        )
        event = await subscription.get()

        self.assertFalse(applied)
        self.assertEqual(link.decision_policy.config.mode, "hybrid")
        self.assertEqual(event.event_type, "runtime.autonomy.ignored")
        bus.close()

    # 验证迟到的 LLM 建议不会覆盖更新后的外部控制
    async def test_rejects_stale_autonomous_revision(self):
        link = FakeLink()
        bus = LinkEventBus()
        api = GalateaRuntimeApi(link, bus)
        await api.update_controls(
            {"autonomy": {"enabled": True}},
            expected_revision=0,
        )

        applied = await api.apply_autonomous_intervention(
            LlmInterventionUpdate(
                mode="core_only",
                ttl_decisions=1,
                base_revision=0,
            ),
            request_id=13,
        )

        self.assertFalse(applied)
        self.assertEqual(link.decision_policy.config.mode, "hybrid")
        bus.close()

    # 验证社交平台消息会进入统一事件流
    async def test_publishes_external_message(self):
        bus = LinkEventBus()
        api = GalateaRuntimeApi(FakeLink(), bus)
        subscription = api.subscribe_events()

        await api.publish_external_message(
            "  开始决斗吧  ",
            source="astrbot.qq",
            sender_id="10001",
        )
        event = await subscription.get()

        self.assertEqual(event.event_type, "external.message.received")
        self.assertEqual(event.payload["text"], "开始决斗吧")
        self.assertEqual(event.payload["source"], "astrbot.qq")
        bus.close()

    # 验证非法运行时介入参数会被拒绝
    async def test_rejects_invalid_intervention(self):
        bus = LinkEventBus()
        api = GalateaRuntimeApi(FakeLink(), bus)

        with self.assertRaisesRegex(ValueError, "介入模式"):
            await api.update_intervention(mode="random")
        with self.assertRaisesRegex(ValueError, "0 到 255"):
            await api.update_intervention(force_llm_message_types=[256])
        with self.assertRaisesRegex(ValueError, "未知字段"):
            await api.update_controls({"autonomy": {"unsafe": True}})
        bus.close()


if __name__ == "__main__":
    unittest.main()
