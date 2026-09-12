# 游戏内聊天测试模块，验证协议编解码、历史限制和运行时反向接口

import unittest
from types import SimpleNamespace

from agents.decision_policy import InterventionPolicy
from app_config import DecisionConfig, GameChatConfig
from core.game_chat import (
    CTOS_CHAT,
    GameChatHistory,
    classify_game_chat_role,
    decode_stoc_chat_payload,
    encode_ctos_chat_payload,
)
from core.link_events import LinkEventBus
from core.network import build_packet
from core.runtime_api import GalateaRuntimeApi


class FakeClient:
    # 初始化记录聊天发送参数的假网络客户端
    def __init__(self):
        self.is_connected = True
        self.sent: list[tuple[str, int, bool]] = []

    # 模拟聊天发送并返回协议规范化文本
    async def send_chat(self, text, *, max_utf16_units, truncate):
        normalized, _ = encode_ctos_chat_payload(
            text,
            max_utf16_units=max_utf16_units,
            truncate=truncate,
        )
        self.sent.append((normalized, max_utf16_units, truncate))
        return normalized


class FakeLink:
    # 初始化运行时接口需要的最小 Link 替身
    def __init__(self):
        self.decision_policy = InterventionPolicy(DecisionConfig())
        self.game_chat_config = GameChatConfig()
        self.game_chat_history = GameChatHistory()
        self.client = FakeClient()
        self.duel_active = True
        self.ai_player_id = 0
        self.decision_coordinator = SimpleNamespace(active_request_id=None)
        self.last_decision_source = None
        self.last_decision_choice_id = None
        self.latest_chat_suggestion = None

    # 返回空观察以满足运行状态读取
    def get_latest_llm_observation(self):
        return None


class GameChatProtocolTests(unittest.TestCase):
    # 验证包含代理对字符的聊天可无损往返
    def test_utf16_round_trip(self):
        normalized, outbound = encode_ctos_chat_payload("你好😀")
        player_type, inbound = decode_stoc_chat_payload(b"\x01\x00" + outbound)
        self.assertEqual(normalized, "你好😀")
        self.assertEqual((player_type, inbound), (1, "你好😀"))
        packet = build_packet(CTOS_CHAT, outbound)
        self.assertEqual(packet[2], CTOS_CHAT)

    # 验证超长文本会拒绝或在允许时安全截断
    def test_length_guard_and_surrogate_safe_truncation(self):
        with self.assertRaises(ValueError):
            encode_ctos_chat_payload("😀😀", max_utf16_units=3)
        normalized, payload = encode_ctos_chat_payload(
            "😀😀",
            max_utf16_units=3,
            truncate=True,
        )
        self.assertEqual(normalized, "😀")
        self.assertTrue(payload.endswith(b"\x00\x00"))

    # 验证畸形服务端聊天不会被静默接受
    def test_invalid_inbound_payload(self):
        with self.assertRaises(ValueError):
            decode_stoc_chat_payload(b"\x01\x00A\x00")
        self.assertEqual(classify_game_chat_role(1, 0), "opponent")
        self.assertEqual(classify_game_chat_role(0, 0), "agent")
        self.assertEqual(classify_game_chat_role(8, 0), "system")


class GameChatRuntimeTests(unittest.IsolatedAsyncioTestCase):
    # 初始化独立运行时和事件订阅
    async def asyncSetUp(self):
        self.link = FakeLink()
        self.bus = LinkEventBus()
        self.runtime = GalateaRuntimeApi(self.link, self.bus)
        self.subscription = self.runtime.subscribe_events()

    # 清理事件订阅
    async def asyncTearDown(self):
        self.subscription.close()
        self.bus.close()

    # 验证入站事件、LLM 上下文和显式出站接口
    async def test_reverse_interface_and_context(self):
        received = self.runtime.record_incoming_game_chat(
            player_type=1,
            role="opponent",
            text="你好",
        )
        self.assertEqual(received["role"], "opponent")
        event = await self.subscription.get()
        self.assertEqual(event.event_type, "game_chat.received")

        context = self.runtime.get_llm_game_chat_context()
        self.assertEqual(context["trust"], "untrusted_social_context")
        self.assertEqual(context["messages"][0]["text"], "你好")

        sent = await self.runtime.send_game_chat("  收到  ", source="astrbot")
        self.assertEqual(sent["text"], "收到")
        self.assertEqual(self.link.client.sent[0], ("收到", 120, False))
        echoed = self.runtime.record_incoming_game_chat(
            player_type=0,
            role="agent",
            text="收到",
        )
        self.assertEqual(echoed["echo_of_sequence"], sent["sequence"])
        self.assertEqual(len(self.runtime.get_game_chat_history()), 2)

    # 验证统一控制面可关闭上下文并开启受节流保护的自动发送
    async def test_unified_controls_and_auto_send(self):
        controls = await self.runtime.update_controls(
            {
                "game_chat": {
                    "include_in_llm_context": False,
                    "auto_send_llm_chat": True,
                    "min_auto_send_interval": 0,
                    "max_outbound_utf16_units": 20,
                }
            },
            expected_revision=0,
        )
        self.assertEqual(controls["revision"], 1)
        self.assertFalse(controls["game_chat"]["include_in_llm_context"])
        self.assertIsNone(self.runtime.get_llm_game_chat_context())

        first = await self.runtime.send_llm_game_chat("打得不错", request_id=3)
        duplicate = await self.runtime.send_llm_game_chat("打得不错", request_id=4)
        self.assertEqual(first["automatic"], True)
        self.assertIsNone(duplicate)
        self.assertEqual(len(self.link.client.sent), 1)


if __name__ == "__main__":
    unittest.main()
