import asyncio
import struct
import sys
import traceback

# === YGOPro / MDPro 核心网络指令常量 ===
CTOS_RESPONSE     = 0x01
CTOS_UPDATE_DECK  = 0x02  
CTOS_HAND_RESULT  = 0x03
CTOS_TP_RESULT    = 0x04
CTOS_TIME_CONFIRM = 0x15  # <--- 修复：心跳包是 0x15 
CTOS_PLAYER_INFO  = 0x10
CTOS_CREATE_GAME  = 0x11
CTOS_JOIN_GAME    = 0x12
CTOS_LEAVE_GAME   = 0x13
CTOS_SURRENDER    = 0x14
CTOS_HS_READY     = 0x22  

STOC_GAME_MSG     = 0x01
STOC_ERROR_MSG    = 0x02
STOC_SELECT_HAND  = 0x03
STOC_SELECT_TP    = 0x04
STOC_HAND_RESULT  = 0x05
STOC_JOIN_GAME    = 0x12  # <--- 修复：加入游戏是 0x12
STOC_TYPE_CHANGE  = 0x13  # <--- 修复：分配座位是 0x13
STOC_DUEL_START   = 0x15  # <--- 修复：决斗开始是 0x15
STOC_DUEL_END     = 0x16  # <--- 修复：决斗结束是 0x16


# 安全输出网络日志并兼容不支持 emoji 的控制台编码
def _console_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_message = message.encode(encoding, errors="replace").decode(encoding)
        print(safe_message)


# 构建带长度头的 YGOPro 网络包
def build_packet(msg_type: int, payload: bytes = b'') -> bytes:
    if not 0 <= msg_type <= 0xFF:
        raise ValueError("消息类型必须位于 0 到 255")
    if not isinstance(payload, bytes):
        raise TypeError("消息载荷必须是 bytes")
    total_len = 1 + len(payload)
    return struct.pack('<H', total_len) + bytes([msg_type]) + payload


# 构建加入房间所需的协议载荷
def build_join_payload(password: str, version: int, game_id: int = 0) -> bytes:
    ver_bytes = struct.pack('<I', version)
    gameid_bytes = struct.pack('<I', game_id)
    pass_bytes = password.encode('utf-16le')[:40].ljust(40, b'\x00')
    return ver_bytes + gameid_bytes + pass_bytes


# 判断消息是否要求当前玩家选择先后手
def is_select_tp_request(msg_type: int, payload: bytes) -> bool:
    return len(payload) == 0 and msg_type in (STOC_SELECT_TP, STOC_HAND_RESULT)

class YgoNetClient:
    def __init__(
        self,
        host,
        port,
        on_msg_callback,
        protocol_version=0x1361,
        game_id=0,
        connect_timeout=10.0,
        inbound_queue_size=256,
        trace_packets=False,
    ):
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None
        self.on_msg_callback = on_msg_callback
        self.is_connected = False
        self.protocol_version = protocol_version
        self.game_id = game_id
        self.connect_timeout = connect_timeout
        self.trace_packets = trace_packets
        self._write_lock = asyncio.Lock()
        self._incoming = asyncio.Queue(maxsize=inbound_queue_size)
        self._receive_task = None
        self._dispatch_task = None

    async def connect(self):
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout,
            )
            self.is_connected = True
            _console_print(f"🔗 成功连接到服务器 {self.host}:{self.port}")
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())
            return True
        except Exception as e:
            _console_print(f"❌ 连接失败: {e}")
            return False

    async def send_packet(self, msg_type: int, payload: bytes = b''):
        """🌟 升级为真异步：确保每一发网络弹药都实时轰向网卡"""
        packet = build_packet(msg_type, payload)

        async with self._write_lock:
            if not self.writer or not self.is_connected:
                return
            if self.trace_packets:
                _console_print(
                    f"[协议发送] CTOS type=0x{msg_type:02X} payload={len(payload)}"
                )
            self.writer.write(packet)
            try:
                await self.writer.drain() # 👑 真正的同步网络强冲刷
            except Exception:
                pass

    async def _receive_loop(self):
        try:
            while self.is_connected:
                header = await self.reader.readexactly(2)
                packet_len = struct.unpack('<H', header)[0]
                payload = await self.reader.readexactly(packet_len)

                msg_type = payload[0]
                msg_data = payload[1:]
                if self.trace_packets:
                    _console_print(
                        f"[协议接收] STOC type=0x{msg_type:02X} payload={len(msg_data)}"
                    )
                await self._incoming.put((msg_type, msg_data))
                
        except asyncio.IncompleteReadError:
            _console_print("⚠️ 服务器断开连接 (IncompleteRead)")
            self.is_connected = False
        except ConnectionResetError:
            _console_print("⚠️ 服务器强制重置了连接")
            self.is_connected = False
        except Exception as e:
            _console_print(f"❌ 网络循环异常: {e}")
            traceback.print_exc()
        finally:
            self.is_connected = False
            try:
                self._incoming.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # 按网络接收顺序调用消息处理器并隔离收包任务
    async def _dispatch_loop(self):
        try:
            while self.is_connected or not self._incoming.empty():
                incoming = await self._incoming.get()
                if incoming is None:
                    self._incoming.task_done()
                    break
                msg_type, msg_data = incoming
                try:
                    await self.on_msg_callback(msg_type, msg_data)
                finally:
                    self._incoming.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _console_print(f"❌ 消息分发循环异常: {e}")
            traceback.print_exc()
            self.is_connected = False

    # 关闭网络连接并回收后台收包任务
    async def close(self):
        self.is_connected = False
        receive_task = self._receive_task
        dispatch_task = self._dispatch_task
        self._receive_task = None
        self._dispatch_task = None
        tasks = []
        for task in (receive_task, dispatch_task):
            if (
                task is not None
                and task is not asyncio.current_task()
                and not task.done()
            ):
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        async with self._write_lock:
            writer = self.writer
            self.reader = None
            self.writer = None
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    # --- 异步快捷发包方法 ---
    async def send_player_info(self, name="Galatea"):
        name_bytes = name.encode('utf-16le')[:40].ljust(40, b'\x00')
        await self.send_packet(CTOS_PLAYER_INFO, name_bytes)

    async def send_join_game(self, password, version=None, game_id=None):
        version = self.protocol_version if version is None else version
        game_id = self.game_id if game_id is None else game_id
        payload = build_join_payload(password, version, game_id)
        await self.send_packet(CTOS_JOIN_GAME, payload)

    async def send_deck(self, main_deck, extra_deck):
        main_len = struct.pack('<I', len(main_deck))
        extra_len = struct.pack('<I', len(extra_deck))
        main_bytes = b''.join([struct.pack('<I', code) for code in main_deck])
        extra_bytes = b''.join([struct.pack('<I', code) for code in extra_deck])
        await self.send_packet(CTOS_UPDATE_DECK, main_len + extra_len + main_bytes + extra_bytes)

    async def send_ready(self):
        await self.send_packet(CTOS_HS_READY)

    async def send_decision(self, decision):
        if decision is None:
            payload = struct.pack('<i', -1)
        elif isinstance(decision, int):
            payload = struct.pack('<i', decision)
        elif isinstance(decision, (bytes, bytearray)):
            payload = bytes(decision)
        else:
            payload = struct.pack('<i', 0)

        _console_print(f"📤 [网络发包] 回复 CTOS_RESPONSE (0x01) -> Hex流: {payload.hex().upper()}")
        await self.send_packet(CTOS_RESPONSE, payload)
