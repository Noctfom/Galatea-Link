import asyncio
import struct
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
STOC_JOIN_GAME    = 0x12  # <--- 修复：加入游戏是 0x12
STOC_TYPE_CHANGE  = 0x13  # <--- 修复：分配座位是 0x13
STOC_DUEL_START   = 0x15  # <--- 修复：决斗开始是 0x15
STOC_DUEL_END     = 0x16  # <--- 修复：决斗结束是 0x16

class YgoNetClient:
    def __init__(self, host, port, on_msg_callback):
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None
        self.on_msg_callback = on_msg_callback
        self.is_connected = False

    async def connect(self):
        try:
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.is_connected = True
            print(f"🔗 成功连接到服务器 {self.host}:{self.port}")
            asyncio.create_task(self._receive_loop())
            return True
        except Exception as e:
            print(f"❌ 连接失败: {e}")
            return False

    async def send_packet(self, msg_type: int, payload: bytes = b''):
        """🌟 升级为真异步：确保每一发网络弹药都实时轰向网卡"""
        if not self.writer or not self.is_connected: return
        total_len = 1 + len(payload)
        header = struct.pack('<H', total_len) 
        packet = header + bytes([msg_type]) + payload
        
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
                await self.on_msg_callback(msg_type, msg_data)
                
        except asyncio.IncompleteReadError:
            print("⚠️ 服务器断开连接 (IncompleteRead)。")
            self.is_connected = False
        except ConnectionResetError:
            print("⚠️ 服务器强制重置了连接。")
            self.is_connected = False
        except Exception as e:
            print(f"❌ 网络循环异常: {e}")
            traceback.print_exc()

    # --- 异步快捷发包方法 ---
    async def send_player_info(self, name="Galatea"):
        name_bytes = name.encode('utf-16le')[:40].ljust(40, b'\x00')
        await self.send_packet(CTOS_PLAYER_INFO, name_bytes)

    async def send_join_game(self, password, version=0x1361):
        ver_bytes = struct.pack('<I', version)
        gameid_bytes = struct.pack('<I', 0)
        pass_bytes = password.encode('utf-16le')[:40].ljust(40, b'\x00')
        await self.send_packet(CTOS_JOIN_GAME, ver_bytes + gameid_bytes + pass_bytes)

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

        print(f"📤 [网络发包] 回复 CTOS_RESPONSE (0x01) -> Hex流: {payload.hex().upper()}")
        await self.send_packet(CTOS_RESPONSE, payload)