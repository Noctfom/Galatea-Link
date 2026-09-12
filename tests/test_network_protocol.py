# 网络协议测试，验证 YGOPro 标准封包和加入房间载荷

import asyncio
import struct
import unittest
from unittest.mock import AsyncMock

from core.network import (
    CTOS_HS_START,
    CTOS_JOIN_GAME,
    PLAYERCHANGE_READY,
    YgoNetClient,
    build_join_payload,
    build_packet,
    build_tp_result,
    describe_win_reason,
    is_select_tp_request,
    parse_duel_player_id,
    parse_error_message,
    parse_lobby_player_change,
    parse_time_limit,
    parse_type_change,
    parse_win_message,
)


class BlockingWriter:
    # 初始化用于控制刷新时机的模拟写入器
    def __init__(self):
        self.writes = []
        self.first_drain_started = asyncio.Event()
        self.release_first_drain = asyncio.Event()
        self.closed = False

    # 记录每次完整写入的网络包
    def write(self, packet):
        self.writes.append(packet)

    # 阻塞首次刷新以验证后续写入会等待发送锁
    async def drain(self):
        if len(self.writes) == 1:
            self.first_drain_started.set()
            await self.release_first_drain.wait()

    # 标记测试连接已经关闭
    def close(self):
        self.closed = True

    # 模拟等待网络连接完成关闭
    async def wait_closed(self):
        return None


class PacketReader:
    # 初始化预设网络片段并在耗尽后模拟断线
    def __init__(self, chunks):
        self.chunks = list(chunks)

    # 按测试顺序返回指定长度的网络片段
    async def readexactly(self, expected_size):
        if not self.chunks:
            raise asyncio.IncompleteReadError(b"", expected_size)
        chunk = self.chunks.pop(0)
        if len(chunk) != expected_size:
            raise AssertionError("模拟网络片段长度不符合预期")
        return chunk


class NetworkProtocolTests(unittest.TestCase):
    # 验证网络包长度包含消息类型和载荷
    def test_build_packet(self):
        packet = build_packet(CTOS_JOIN_GAME, b"abc")
        self.assertEqual(packet, b"\x04\x00\x12abc")

    # 验证加入房间载荷使用配置化版本和房间编号
    def test_build_join_payload(self):
        payload = build_join_payload("测试", 0x1361, 9)

        self.assertEqual(len(payload), 48)
        self.assertEqual(struct.unpack("<I", payload[:4])[0], 0x1361)
        self.assertEqual(struct.unpack("<I", payload[4:8])[0], 9)
        self.assertEqual(payload[8:12].decode("utf-16le"), "测试")

    # 验证越界消息类型会在发包前被拒绝
    def test_rejects_invalid_message_type(self):
        with self.assertRaises(ValueError):
            build_packet(256)

    # 验证标准与旧兼容选先后手消息不会误判正常猜拳结果
    def test_select_tp_request_compatibility(self):
        self.assertTrue(is_select_tp_request(0x04, b""))
        self.assertTrue(is_select_tp_request(0x05, b""))
        self.assertFalse(is_select_tp_request(0x05, b"\x01\x02"))

    # 验证先攻偏好会让猜拳胜者选择自己先攻
    def test_first_turn_preference_uses_winner_first_value(self):
        self.assertEqual(build_tp_result(False), 1)

    # 验证后攻偏好会让猜拳胜者选择自己后攻
    def test_second_turn_preference_uses_winner_second_value(self):
        self.assertEqual(build_tp_result(True), 0)

    # 验证非法先后攻偏好不会被静默转换
    def test_tp_result_rejects_non_boolean_preference(self):
        with self.assertRaises(TypeError):
            build_tp_result(1)

    # 验证 MSG_START 能区分当前客户端在 Core 中的先后攻编号
    def test_parses_duel_player_id_from_start_message(self):
        self.assertEqual(parse_duel_player_id(b"\x00rest"), 0)
        self.assertEqual(parse_duel_player_id(b"\x01rest"), 1)
        self.assertIsNone(parse_duel_player_id(b"\x10rest"))

    # 验证标准四字节计时包会跳过结构体对齐字节
    def test_parses_standard_time_limit_payload(self):
        self.assertEqual(parse_time_limit(b"\x01\x00\x2c\x01"), (1, 300))

    # 验证三字节紧凑计时包仍可兼容解析
    def test_parses_packed_time_limit_payload(self):
        self.assertEqual(parse_time_limit(b"\x00\x78\x00"), (0, 120))

    # 验证 srvpro 标准对齐错误包能返回协议拒绝版本
    def test_parses_aligned_protocol_version_error(self):
        payload = b"\x04\x00\x00\x00" + struct.pack("<I", 0x1372)
        self.assertEqual(parse_error_message(payload), (4, 0x1372))

    # 验证紧凑错误包仍能返回协议拒绝版本
    def test_parses_packed_protocol_version_error(self):
        payload = b"\x04" + struct.pack("<I", 0x1372)
        self.assertEqual(parse_error_message(payload), (4, 0x1372))

    # 验证客户端加入房间时使用实例配置的协议参数
    def test_client_uses_configured_join_values(self):
        client = YgoNetClient(
            "127.0.0.1",
            7911,
            AsyncMock(),
            protocol_version=0x2468,
            game_id=11,
        )
        client.send_packet = AsyncMock()

        async def run_send():
            # 执行一次加入房间发包以捕获最终载荷
            await client.send_join_game("room")

        import asyncio

        asyncio.run(run_send())
        payload = client.send_packet.await_args.args[1]
        self.assertEqual(client.send_packet.await_args.args[0], CTOS_JOIN_GAME)
        self.assertEqual(struct.unpack("<I", payload[:4])[0], 0x2468)
        self.assertEqual(struct.unpack("<I", payload[4:8])[0], 11)

    # 验证大厅身份消息能同时识别座位和房主标记
    def test_parses_lobby_host_role(self):
        self.assertEqual(parse_type_change(b"\x10"), (0, True))
        self.assertEqual(parse_type_change(b"\x01"), (1, False))

    # 验证大厅玩家状态能拆分座位和准备状态
    def test_parses_lobby_ready_state(self):
        self.assertEqual(parse_lobby_player_change(b"\x19"), (1, PLAYERCHANGE_READY))

    # 验证胜负消息保留赢家和准确原因代码
    def test_parses_win_message_and_reason(self):
        self.assertEqual(parse_win_message(b"\x01\x04"), (1, 4))
        self.assertEqual(describe_win_reason(4), "失去连接")
        self.assertEqual(describe_win_reason(0x10), "特殊胜利 0x10")

    # 验证网络客户端可以发送标准房主开始指令
    def test_client_sends_host_start_packet(self):
        client = YgoNetClient("127.0.0.1", 7911, AsyncMock())
        client.send_packet = AsyncMock()

        asyncio.run(client.send_start_duel())

        client.send_packet.assert_awaited_once_with(CTOS_HS_START)


class NetworkConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    # 验证并发心跳和决策发包不会越过正在刷新的网络包
    async def test_serializes_concurrent_writes(self):
        writer = BlockingWriter()
        client = YgoNetClient("127.0.0.1", 7911, AsyncMock())
        client.writer = writer
        client.is_connected = True

        first_send = asyncio.create_task(client.send_packet(0x15))
        await writer.first_drain_started.wait()
        second_send = asyncio.create_task(client.send_packet(0x01, b"action"))
        await asyncio.sleep(0)

        self.assertEqual(len(writer.writes), 1)

        writer.release_first_drain.set()
        await asyncio.gather(first_send, second_send)
        self.assertEqual(len(writer.writes), 2)

    # 验证慢速消息处理器不会阻塞后续网络包读取
    async def test_receive_loop_isolated_from_dispatcher(self):
        first_callback_started = asyncio.Event()
        release_first_callback = asyncio.Event()
        received = []

        async def callback(msg_type, msg_data):
            # 阻塞首条消息以观察收包任务是否继续工作
            received.append((msg_type, msg_data))
            if len(received) == 1:
                first_callback_started.set()
                await release_first_callback.wait()

        client = YgoNetClient("127.0.0.1", 7911, callback)
        client.reader = PacketReader(
            [
                b"\x02\x00",
                b"\x10a",
                b"\x02\x00",
                b"\x11b",
            ]
        )
        client.is_connected = True
        receive_task = asyncio.create_task(client._receive_loop())
        dispatch_task = asyncio.create_task(client._dispatch_loop())
        client._receive_task = receive_task
        client._dispatch_task = dispatch_task

        await first_callback_started.wait()
        await receive_task
        self.assertEqual(client._incoming.qsize(), 2)

        release_first_callback.set()
        await dispatch_task
        self.assertEqual(received, [(0x10, b"a"), (0x11, b"b")])


if __name__ == "__main__":
    unittest.main()
