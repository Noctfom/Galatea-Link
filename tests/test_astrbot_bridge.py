# AstrBot 远程桥接测试，验证容器友好接口、会话归属和事件转发

import asyncio
import json
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestServer

from app_config import ServiceConfig
from core.link_events import LinkEventBus
from service.http_api import create_http_app
from service.session_manager import LinkSessionManager
from tests.astrbot_plugin_duel_galatea_stage.galatea_link_bridge import (
    GalateaBridgeSettings,
    GalateaLinkBridge,
)


class FakeRuntime:
    # 初始化远程桥接使用的测试运行时
    def __init__(self, events: LinkEventBus) -> None:
        self.events = events
        self.revision = 0
        self.mode = "hybrid"
        self.threshold = 0.65
        self.core_policy_mode = "greedy"
        self.core_temperature = 0.8
        self.history = []

    # 创建 Link 事件订阅
    def subscribe_events(self, max_queue_size=128):
        return self.events.subscribe(max_queue_size=max_queue_size)

    # 返回脱敏测试运行状态
    def get_status(self):
        return {
            "connected": True,
            "duel_active": False,
            "core_model": {"available": True},
            "decision": {
                "mode": self.mode,
                "core_confidence_threshold": self.threshold,
            },
        }

    # 返回测试宏观控制快照
    def get_controls(self):
        intervention = {
            "mode": self.mode,
            "core_confidence_threshold": self.threshold,
            "core_policy_mode": self.core_policy_mode,
            "core_temperature": self.core_temperature,
        }
        return {
            "revision": self.revision,
            "intervention": intervention,
            "baseline_intervention": dict(intervention),
            "autonomy": {"enabled": False},
            "game_chat": {"enabled": True},
        }

    # 应用测试宏观控制补丁
    async def update_controls(self, patch, *, source, expected_revision=None):
        if expected_revision is not None and expected_revision != self.revision:
            raise RuntimeError("宏观控制版本冲突")
        intervention = patch.get("intervention", {})
        self.mode = intervention.get("mode", self.mode)
        self.threshold = intervention.get(
            "core_confidence_threshold",
            self.threshold,
        )
        self.core_policy_mode = intervention.get(
            "core_policy_mode",
            self.core_policy_mode,
        )
        self.core_temperature = intervention.get(
            "core_temperature",
            self.core_temperature,
        )
        self.revision += 1
        return self.get_controls()

    # 返回固定可见观察
    def get_latest_observation(self):
        return {"observation_id": 1}

    # 记录远程聊天发送
    async def send_game_chat(self, text, *, source):
        item = {"role": "agent", "text": text, "source": source}
        self.history.append(item)
        return item

    # 返回最近远程聊天记录
    def get_game_chat_history(self, max_messages=12):
        return self.history[-max_messages:]

    # 记录并发布外部社交消息测试事件
    async def publish_external_message(self, text, *, source, sender_id=None):
        return self.events.publish(
            "external.message.received",
            {
                "text": text,
                "source": source,
                "sender_id": sender_id,
            },
        )


class FakeLink:
    # 初始化可由独立服务持有的测试 Link
    def __init__(self) -> None:
        self.events = LinkEventBus()
        self.runtime = FakeRuntime(self.events)
        self.closed = asyncio.Event()

    # 模拟持续运行的游戏连接
    async def start(self):
        await self.closed.wait()

    # 关闭测试游戏连接和内部事件流
    async def close(self):
        if self.closed.is_set():
            return
        self.events.publish("link.closed")
        self.events.close()
        self.closed.set()


class FakeLogger:
    # 接收桥接错误日志
    def error(self, message):
        return None

    # 接收桥接异常日志
    def exception(self, message):
        return None

    # 接收可恢复的桥接警告日志
    def warning(self, message):
        return None


class AstrBotRemoteBridgeTests(unittest.IsolatedAsyncioTestCase):
    # 为每个测试启动真实 HTTP 服务和假 Link 运行时
    async def asyncSetUp(self):
        self.link = FakeLink()
        self.manager = LinkSessionManager(
            Path("config.yaml"),
            runtime_builder=lambda _: self.link,
        )
        app = create_http_app(
            self.manager,
            ServiceConfig(api_token="bridge-token"),
        )
        self.server = TestServer(app)
        await self.server.start_server()
        settings = GalateaBridgeSettings.from_mapping(
            {
                "galatea_link": {
                    "enabled": True,
                    "base_url": str(self.server.make_url("/")).rstrip("/"),
                    "api_token": "bridge-token",
                    "reconnect_delay": 0.5,
                }
            }
        )
        self.notifications = []

        # 保存主动通知供测试断言
        async def notify(umo, text):
            self.notifications.append((umo, text))

        self.bridge = GalateaLinkBridge(settings, notify, FakeLogger())

    # 清理桥接和独立服务后台资源
    async def asyncTearDown(self):
        await self.bridge.terminate()
        await self.server.close()

    # 验证远程启动、状态、控制和聊天形成完整闭环
    async def test_remote_control_contract(self):
        status = await self.bridge.start("qq:group:100")
        self.assertTrue(status["running"])
        refreshed = await self.bridge.refresh_status()
        self.assertTrue(refreshed["runtime"]["connected"])

        intervention = await self.bridge.update_mode(
            "qq:group:100",
            "llm_review",
        )
        self.assertEqual(intervention["mode"], "llm_review")
        intervention = await self.bridge.update_threshold("qq:group:100", 0.4)
        self.assertEqual(intervention["core_confidence_threshold"], 0.4)
        policy = await self.bridge.update_core_policy(
            "qq:group:100",
            "deployment",
        )
        self.assertEqual(policy["core_policy_mode"], "deployment")
        temperature = await self.bridge.update_core_temperature(
            "qq:group:100",
            1.2,
        )
        self.assertEqual(temperature["core_temperature"], 1.2)
        await self.bridge.send_game_chat("qq:group:100", "你好")
        history = await self.bridge.get_game_chat_history("qq:group:100")
        self.assertEqual(history[-1]["text"], "你好")

    # 验证桥接器可以在对局启动前读取配置和模型仓库
    async def test_reads_configuration_and_model_catalog(self):
        configuration = await self.bridge.get_configuration()
        self.assertIn("server", configuration)
        self.assertNotIn("password", configuration["server"])
        self.assertIn("available_decks", configuration)

        catalog = await self.bridge.get_models()
        self.assertEqual(catalog["supported_model_protocols"], [3])
        self.assertIn("selection", catalog)
        decks = await self.bridge.get_decks("group_100")
        self.assertEqual(decks["scope"], "current_astrbot_session")
        self.assertTrue(decks["decks"])
        self.assertIn("cards", decks["decks"][0])

    # 验证插件空闲时也通过心跳显示 Link 已连接
    async def test_idle_heartbeat_reports_link_connection(self):
        await self.bridge.initialize()
        for _ in range(40):
            if self.bridge.get_status()["link_reachable"]:
                break
            await asyncio.sleep(0.01)
        status = self.bridge.get_status()
        self.assertEqual(status["connection_state"], "connected")
        self.assertTrue(status["link_reachable"])
        remote = self.manager.get_status()["integrations"]["astrbot"]
        self.assertTrue(remote["connected"])

    # 验证插件 Pages 使用与后端注册一致的 link 路由并展示全部设置
    async def test_plugin_page_routes_and_settings_schema(self):
        plugin_root = Path(__file__).parent / "astrbot_plugin_duel_galatea_stage"
        script = (plugin_root / "pages/link-control/app.js").read_text(
            encoding="utf-8"
        )
        schema = json.loads(
            (plugin_root / "_conf_schema.json").read_text(encoding="utf-8")
        )["galatea_link"]["items"]
        self.assertIn('apiGet("link/status")', script)
        self.assertIn('apiGet("link/settings")', script)
        self.assertIn('apiPost("link/settings/save"', script)
        self.assertTrue(
            {
                "enabled",
                "base_url",
                "api_token",
                "api_token_env",
                "request_timeout",
                "reconnect_delay",
                "heartbeat_interval",
                "auto_connect",
                "instance_name",
                "forward_game_chat",
                "forward_lifecycle_events",
                "forward_decision_errors",
                "forward_chat_suggestions",
                "remote_agent_enabled",
                "agent_game_chat_enabled",
                "inject_duel_summary",
                "allow_agent_deck_edit",
            }.issubset(schema)
        )
        self.assertIn("allowAgentDeckEdit", script)
        self.assertIn("agentGameChatEnabled", script)
        self.assertIn("decisionTimeBudget", script)
        self.assertIn("saveToast", script)

    # 验证桥接器能读取对局观察并投递 QQ 社交消息
    async def test_external_message_and_observation_bridge(self):
        await self.bridge.start("qq:group:100")
        observation = await self.bridge.get_latest_observation("qq:group:100")
        self.assertEqual(observation["observation_id"], 1)
        event = await self.bridge.publish_external_message(
            "qq:group:100",
            "请下一回合注意墓地",
            sender_id="user-1",
        )
        self.assertEqual(event["payload"]["source"], "astrbot.qq")

    # 验证远程决策事件可以异步交给 AstrBot 处理器
    async def test_dispatches_remote_decision_event(self):
        received = []

        async def handle_decision(owner, payload):
            received.append((owner, dict(payload)))

        self.bridge._decision_handler = handle_decision
        await self.bridge.start("qq:group:100")
        for _ in range(40):
            if self.bridge._last_event_type == "service.connected":
                break
            await asyncio.sleep(0.01)
        self.link.events.publish(
            "agent.decision.requested",
            {
                "request_id": 12,
                "session_id": "session-1",
                "observation_id": 4,
                "observation": {"decision_required": True},
            },
        )
        for _ in range(40):
            if received:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(received[0][0], "qq:group:100")
        self.assertEqual(received[0][1]["request_id"], 12)

    # 验证关闭事件和更新请求会取消旧决策且不会形成串行积压
    async def test_cancels_closed_and_superseded_remote_decisions(self):
        started = {21: asyncio.Event(), 22: asyncio.Event()}
        cancelled = {21: asyncio.Event(), 22: asyncio.Event()}

        async def handle_decision(owner, payload):
            request_id = int(payload["request_id"])
            started[request_id].set()
            try:
                await asyncio.Future()
            finally:
                cancelled[request_id].set()

        self.bridge._decision_handler = handle_decision
        first = {
            "session_id": "session-1",
            "request_id": 21,
            "expires_at": time.time() + 30,
        }
        second = {
            "session_id": "session-1",
            "request_id": 22,
            "expires_at": time.time() + 30,
        }
        await self.bridge._forward_event(
            "qq:group:100",
            {"event_type": "agent.decision.requested", "payload": first},
        )
        await asyncio.wait_for(started[21].wait(), timeout=1)
        await self.bridge._forward_event(
            "qq:group:100",
            {"event_type": "agent.decision.requested", "payload": second},
        )
        await asyncio.wait_for(cancelled[21].wait(), timeout=1)
        await asyncio.wait_for(started[22].wait(), timeout=1)
        self.assertEqual(list(self.bridge._decision_tasks), [("session-1", 22)])

        await self.bridge._forward_event(
            "qq:group:100",
            {
                "event_type": "agent.decision.closed",
                "payload": {"session_id": "session-1", "request_id": 22},
            },
        )
        await asyncio.wait_for(cancelled[22].wait(), timeout=1)
        self.assertFalse(self.bridge._decision_tasks)

    # 验证远程决策到达绝对期限后会取消处理器
    async def test_remote_decision_deadline_cancels_handler(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handle_decision(owner, payload):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.bridge._decision_handler = handle_decision
        await self.bridge._forward_event(
            "qq:group:100",
            {
                "event_type": "agent.decision.requested",
                "payload": {
                    "session_id": "session-1",
                    "request_id": 31,
                    "expires_at": time.time() + 0.35,
                },
            },
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await asyncio.sleep(0)
        self.assertFalse(self.bridge._decision_tasks)

    # 验证桥接器能保存停止状态的 Agent 非敏感设置
    async def test_updates_configuration_without_binding_owner(self):
        updated = await self.bridge.update_configuration(
            {"agent": {"name": "Galatea_Astr"}},
        )
        self.assertEqual(updated["agent"]["name"], "Galatea_Astr")

    # 验证 WebSocket 游戏聊天会主动回传到 AstrBot 会话
    async def test_forwards_remote_events(self):
        await self.bridge.start("qq:group:100")
        for _ in range(40):
            if self.bridge._last_event_type == "service.connected":
                break
            await asyncio.sleep(0.01)
        self.link.events.publish(
            "game_chat.received",
            {
                "role": "opponent",
                "text": "来决斗吧",
                "echo_of_sequence": None,
            },
        )
        for _ in range(40):
            if self.notifications:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(
            self.notifications,
            [("qq:group:100", "💬 [游戏内·对手] 来决斗吧")],
        )

    # 验证同序号聊天只触发一次主智能体并生成低开销对局摘要
    async def test_deduplicates_game_chat_and_builds_duel_summary(self):
        received = []
        summaries = []

        async def handle_chat(owner, payload):
            received.append((owner, dict(payload)))

        async def handle_summary(owner, payload):
            summaries.append((owner, dict(payload)))

        self.bridge._game_chat_handler = handle_chat
        self.bridge._duel_summary_handler = handle_summary
        await self.bridge.start("qq:group:100")
        for _ in range(40):
            if self.bridge._last_event_type == "service.connected":
                break
            await asyncio.sleep(0.01)
        self.link.events.publish("duel.started")
        chat_payload = {
            "sequence": 8,
            "role": "opponent",
            "text": "你好",
            "echo_of_sequence": None,
        }
        self.link.events.publish("game_chat.received", chat_payload)
        self.link.events.publish("game_chat.received", chat_payload)
        self.link.events.publish(
            "decision.committed",
            {"source": "astrbot", "choice_id": 1},
        )
        self.link.events.publish(
            "observation.updated",
            {"turn": {"number": 2, "phase_name": "主要阶段一"}},
        )
        self.link.events.publish("game_chat.sent", {"text": "你好"})
        self.link.events.publish(
            "duel.ended",
            {
                "server_message_type": 22,
                "outcome": "self_win",
                "winner_role": "self",
                "reason_code": 0,
                "reason": "投降",
                "termination": "server_duel_end",
            },
        )
        for _ in range(80):
            if summaries:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1]["sequence"], 8)
        self.assertEqual(
            [item for item in self.notifications if "你好" in item[1]],
            [("qq:group:100", "💬 [游戏内·对手] 你好")],
        )
        summary = summaries[0][1]
        self.assertEqual(summary["decision_count"], 1)
        self.assertEqual(summary["decision_sources"], {"astrbot": 1})
        self.assertEqual(summary["incoming_chat_count"], 1)
        self.assertEqual(summary["outgoing_chat_count"], 1)
        self.assertEqual(summary["last_turn"], 2)
        self.assertEqual(summary["result"]["outcome"], "self_win")
        self.assertEqual(summary["result"]["reason"], "投降")
        self.assertIn(
            ("qq:group:100", "🏁 Galatea 对局已经结束：Galatea 胜利（投降）"),
            self.notifications,
        )

    # 验证异常断线也会完成一次胜负未知的低开销摘要
    async def test_connection_close_finishes_unknown_duel_summary(self):
        summaries = []

        async def handle_summary(owner, payload):
            summaries.append((owner, dict(payload)))

        self.bridge._duel_summary_handler = handle_summary
        await self.bridge._forward_event(
            "qq:group:100",
            {"event_type": "duel.started", "payload": {}},
        )
        await self.bridge._forward_event(
            "qq:group:100",
            {"event_type": "link.closed", "payload": {}},
        )

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0][1]["result"]["outcome"], "unknown")
        self.assertEqual(
            summaries[0][1]["result"]["reason"],
            "Link 连接关闭",
        )

    # 验证胜负消息立即保存摘要且后续结束包不会重复生成
    async def test_duel_result_immediately_finishes_summary_once(self):
        summaries = []

        async def handle_summary(owner, payload):
            summaries.append((owner, dict(payload)))

        self.bridge._duel_summary_handler = handle_summary
        await self.bridge._forward_event(
            "qq:group:100",
            {"event_type": "duel.started", "payload": {}},
        )
        result_payload = {
            "outcome": "opponent_win",
            "winner_role": "opponent",
            "reason_code": 1,
            "reason": "基本分归零",
            "termination": "engine_result",
        }
        await self.bridge._forward_event(
            "qq:group:100",
            {"event_type": "duel.result", "payload": result_payload},
        )
        await self.bridge._forward_event(
            "qq:group:100",
            {"event_type": "duel.ended", "payload": result_payload},
        )

        self.assertEqual(len(summaries), 1)
        self.assertEqual(
            summaries[0][1]["result"]["outcome"],
            "opponent_win",
        )
        result_notices = [
            message for _, message in self.notifications if message.startswith("🏁")
        ]
        self.assertEqual(len(result_notices), 1)

    # 验证不同 AstrBot 会话不能接管当前插件绑定
    async def test_rejects_other_owner(self):
        await self.bridge.start("qq:group:100")
        with self.assertRaises(PermissionError):
            await self.bridge.update_mode("qq:group:200", "core_only")

    # 验证插件卸载只断开客户端且不会停止独立 Link
    async def test_terminate_keeps_remote_link_running(self):
        await self.bridge.start("qq:group:100")
        await self.bridge.terminate()

        self.assertTrue(self.manager.get_status()["running"])

    # 验证远程停止会清理插件会话归属
    async def test_stop_cleans_owner(self):
        await self.bridge.start("qq:group:100")
        stopped = await self.bridge.stop("qq:group:100")

        self.assertTrue(stopped)
        self.assertFalse(self.bridge.get_status()["running"])
        self.assertIsNone(self.bridge.owner_umo)


if __name__ == "__main__":
    unittest.main()
