# 对局协议路由测试，验证先后攻映射、交互归属和计时确认

import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.gamestate import DuelState
from core.network import (
    CTOS_TIME_CONFIRM,
    PLAYERCHANGE_READY,
    STOC_HS_PLAYER_CHANGE,
    STOC_TIME_LIMIT,
)
from galatea_link import GalateaLink


class DuelProtocolRoutingTests(unittest.IsolatedAsyncioTestCase):
    # 构建绕过模型加载且只保留协议处理依赖的 Link
    def _make_link(self):
        link = GalateaLink.__new__(GalateaLink)
        link.ai_player_id = 1
        link.ai_core_player_id = 0
        link.ai = SimpleNamespace(player_id=0)
        link.deck = SimpleNamespace(main=[100, 200], extra=[300], side=[400])
        link.gamestate = DuelState(
            p0_main=link.deck.main,
            p0_extra=link.deck.extra,
        )
        link.client = SimpleNamespace(
            is_connected=True,
            send_packet=AsyncMock(),
            send_deck=AsyncMock(),
            send_ready=AsyncMock(),
            send_start_duel=AsyncMock(),
        )
        link.duel_active = True
        link.is_room_host = False
        link.room_duel_mode = 0
        link.room_ready = {0: False, 1: False, 2: False, 3: False}
        link._ready_seat = None
        link._start_requested = False
        link._pending_duel_result = None
        link.last_duel_result = None
        link.last_server_error = None
        link.last_deck_submission = None
        link.time_player = None
        link.time_left = {0: None, 1: None}
        link.ignore_actions_blacklist = []
        link.ignore_choice_ids_blacklist = []
        link.last_decision = None
        link.last_decision_choice_id = None
        link.last_prompt_type = None
        link.last_prompt_msg = None
        link._publish_event = Mock()
        link._prepare_prompt_snapshot = AsyncMock(
            return_value=SimpleNamespace(valid_actions=[object()])
        )
        link._capture_llm_observation = Mock()
        link._schedule_decision = AsyncMock()
        return link

    # 验证大厅座位一号收到 Core 玩家零号的完整提示时仍会响应
    async def test_complete_prompt_is_owned_by_receiving_duelist(self):
        link = self._make_link()
        prompt_payload = bytes([0]) + struct.pack("<I", 123)

        await link.handle_server_msg(0x01, bytes([13]) + prompt_payload)

        link._schedule_decision.assert_awaited_once()
        self.assertEqual(link.ai_core_player_id, 0)

    # 验证完整指示物提示会先校正玩家映射再交给宏动作生成
    async def test_complete_counter_prompt_routes_through_macro_actions(self):
        link = self._make_link()
        link.ai_core_player_id = 0

        # 模拟宏动作生成器把完整分配方案写回合法动作池
        async def prepare_counter_prompt(msg_type, msg_payload):
            link.gamestate.current_valid_actions = [object()]
            return SimpleNamespace(valid_actions=[object()])

        link._prepare_prompt_snapshot = AsyncMock(side_effect=prepare_counter_prompt)
        card = struct.pack("<I", 12345678) + bytes([1, 4, 0]) + struct.pack("<H", 2)
        prompt_payload = bytes([1]) + struct.pack("<HHB", 1, 1, 1) + card

        await link.handle_server_msg(0x01, bytes([22]) + prompt_payload)

        self.assertEqual(link.ai_core_player_id, 1)
        link._schedule_decision.assert_awaited_once()

    # 验证 MSG_START 会把后攻客户端映射为 Core 玩家一号
    async def test_start_message_updates_core_player_mapping(self):
        link = self._make_link()

        await link.handle_server_msg(0x01, bytes([4, 1, 5, 0, 0]))

        self.assertEqual(link.ai_core_player_id, 1)
        self.assertEqual(link.ai.player_id, 1)
        self.assertEqual(link.gamestate.p1_deck, link.deck.main)

    # 验证只有自己计时时才回复时间确认
    async def test_time_confirm_only_for_own_core_player(self):
        link = self._make_link()

        await link.handle_server_msg(
            STOC_TIME_LIMIT,
            bytes([1, 0]) + struct.pack("<H", 180),
        )
        link.client.send_packet.assert_not_awaited()

        await link.handle_server_msg(
            STOC_TIME_LIMIT,
            bytes([0, 0]) + struct.pack("<H", 179),
        )
        link.client.send_packet.assert_awaited_once_with(CTOS_TIME_CONFIRM)

    # 验证协议版本拒绝会记录服务端返回的目标版本
    async def test_version_error_records_server_required_version(self):
        link = self._make_link()
        link.configured_protocol_version = 0x1361
        link.active_protocol_version = 0x1361
        link.auto_negotiate_version = True
        link.protocol_version_retry_count = 0
        link.max_version_retries = 1
        link._required_protocol_version = None

        await link.handle_server_msg(
            0x02,
            b"\x04\x00\x00\x00" + struct.pack("<I", 0x1372),
        )

        self.assertEqual(link._required_protocol_version, 0x1372)
        event_payload = link._publish_event.call_args.args[1]
        self.assertEqual(event_payload["required_version"], 0x1372)

    # 验证加入房间时按主额外备牌三区提交完整卡组
    async def test_join_submits_complete_deck(self):
        link = self._make_link()

        await link.handle_server_msg(0x12, bytes(20))

        link.client.send_deck.assert_awaited_once_with(
            [100, 200],
            [300],
            [400],
        )
        self.assertEqual(
            link.last_deck_submission["counts"],
            {"main": 2, "extra": 1, "side": 1},
        )

    # 验证禁限卡错误会解除准备锁并公开具体拒绝原因
    async def test_deck_error_exposes_reason_and_allows_ready_retry(self):
        link = self._make_link()
        link.ai_player_id = 0
        link._ready_seat = 0
        error_code = (1 << 28) | 89631139

        await link.handle_server_msg(
            0x02,
            b"\x02\x00\x00\x00" + struct.pack("<I", error_code),
        )

        self.assertIsNone(link._ready_seat)
        self.assertEqual(link.last_server_error["category"], "deck_rejected")
        self.assertEqual(link.last_server_error["card_code"], 89631139)
        self.assertIn("禁限卡表", link.last_server_error["reason"])
        event_payload = link._publish_event.call_args.args[1]
        self.assertEqual(event_payload["deck"]["counts"]["extra"], 1)

    # 验证房主在双方准备后只发送一次开始指令
    async def test_room_host_starts_after_all_players_ready(self):
        link = self._make_link()
        link.duel_active = False

        await link.handle_server_msg(0x13, b"\x10")
        await link.handle_server_msg(
            STOC_HS_PLAYER_CHANGE,
            bytes([PLAYERCHANGE_READY]),
        )
        link.client.send_start_duel.assert_not_awaited()

        await link.handle_server_msg(
            STOC_HS_PLAYER_CHANGE,
            bytes([0x10 | PLAYERCHANGE_READY]),
        )
        await link.handle_server_msg(
            STOC_HS_PLAYER_CHANGE,
            bytes([0x10 | PLAYERCHANGE_READY]),
        )

        link.client.send_ready.assert_awaited_once()
        link.client.send_start_duel.assert_awaited_once()
        self.assertTrue(link._start_requested)

    # 验证 Core 胜负消息会转换为己方胜利和结束原因
    async def test_captures_self_win_and_finishes_duel(self):
        link = self._make_link()
        link.ai_core_player_id = 0
        link._reset_duel_state = AsyncMock()

        link._capture_duel_result(b"\x00\x00")
        await link._finish_duel("server_duel_end", server_message_type=0x16)

        self.assertEqual(link.last_duel_result["outcome"], "self_win")
        self.assertEqual(link.last_duel_result["reason"], "投降")
        self.assertEqual(link.last_duel_result["server_message_type"], 0x16)
        link._reset_duel_state.assert_awaited_once()

    # 验证没有胜负消息的中途断线会明确记录胜负未知
    async def test_connection_loss_finishes_with_unknown_result(self):
        link = self._make_link()
        link._reset_duel_state = AsyncMock()

        await link._finish_duel("connection_lost")

        self.assertEqual(link.last_duel_result["outcome"], "unknown")
        self.assertEqual(link.last_duel_result["reason"], "连接中断")

    # 验证连接循环只按服务端目标版本执行一次有限重连
    async def test_start_retries_once_with_server_required_version(self):
        link = GalateaLink.__new__(GalateaLink)
        link._closed = False
        link.auto_negotiate_version = True
        link.max_version_retries = 1
        link.protocol_version_retry_count = 0
        link.negotiated_protocol_version = None
        link._required_protocol_version = None
        link._publish_event = Mock()
        link.close = AsyncMock()

        # 使用可记录每次 Join 版本的轻量网络替身
        class RetryClient:
            def __init__(self):
                self.host = "duel.example"
                self.port = 7911
                self.protocol_version = 0x1361
                self.is_connected = False
                self.connect_count = 0
                self.join_versions = []

            # 模拟每次 TCP 连接成功
            async def connect(self):
                self.connect_count += 1
                self.is_connected = True
                return True

            # 接收测试玩家名但不产生网络写入
            async def send_player_info(self, name):
                return None

            # 记录本次加入房间实际声明的版本
            async def send_join_game(self, password):
                self.join_versions.append(self.protocol_version)

            # 首次断开前返回目标版本且第二次正常结束
            async def wait_for_disconnect(self):
                if self.connect_count == 1:
                    link._required_protocol_version = 0x1372
                self.is_connected = False

            # 模拟清理已结束的连接
            async def close(self):
                self.is_connected = False

        link.client = RetryClient()
        link.password = "room"
        link.agent_name = "Galatea"

        await GalateaLink.start(link)

        self.assertEqual(link.client.connect_count, 2)
        self.assertEqual(link.client.join_versions, [0x1361, 0x1372])
        self.assertEqual(link.negotiated_protocol_version, 0x1372)
        link.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
