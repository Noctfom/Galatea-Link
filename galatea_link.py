import asyncio
import struct
import traceback
import io
from net_client import *
from ai_bot import AiBot
from gamestate import DuelState, MessageParser
from deck_utils import load_deck
from rule_bot import get_rule_decision, sync_valid_actions
from card_reader import card_db

class GalateaLink:
    def __init__(self, host, port, password, deck_name):
        self.client = YgoNetClient(host, port, self.handle_server_msg)
        self.password = password
        
        print("🤖 正在唤醒 Galatea AI...")
        self.ai = AiBot(device='cpu') 
        self.ai.env = None 
        self.ai.load_model('./models/galatea_iter_110.pth')
        
        print(f"🃏 正在加载卡组: {deck_name}")
        self.deck = load_deck('./decks', deck_name)
        if self.deck is None:
            raise FileNotFoundError(f"\\n❌ 找不到卡组文件！")
            
        second_hand_keywords = ["后手", "Going Second", "Second Hand", "后攻", "后手位", "后攻位"]
        self.prefer_second = any(kw in deck_name for kw in second_hand_keywords)
        print(f"🎲 战术偏好: {'后攻 (Going Second)' if self.prefer_second else '先攻 (Going First)'}")
        
        self.gamestate = DuelState(
            p0_main=self.deck.main, p0_extra=self.deck.extra,
            p1_main=[], p1_extra=[]
        )
        self.ignore_actions_blacklist = [] 
        
        # 🌟 新增：动态座位ID守护者。默认设为 None，进房后由服务器指派
        self.ai_player_id = None 

    async def start(self):
        if await self.client.connect():
            await self.client.send_player_info("Galatea_AI")
            await self.client.send_join_game(self.password)
            while self.client.is_connected:
                await asyncio.sleep(1)

    def robust_parse(self, data):
        msgs = []
        stream = io.BytesIO(data)
        data_len = len(data)
        last_pos = -1
        while stream.tell() < data_len:
            start = stream.tell()

            if start == last_pos:
                # 如果游标没有前进，说明碰到了未知指令，直接拦截并打印犯罪证据！
                stream.seek(start)
                stuck_type = stream.read(1)[0]
                print(f"\n🚨 [死循环拦截] 解析器卡死在未知 OCG Type: {stuck_type}")
                print(f"📦 完整数据包 Hex: {msg_data.hex()}")
                break 
            last_pos = start

            start_pos = stream.tell()
            msg_type = data[start_pos]
            stream.read(1)
            
            if msg_type in [4, 6, 8]:
                length = data_len - start_pos - 1
            else:
                length = MessageParser.MSG_LEN.get(msg_type, -1)
                if length == -1:
                    try:
                        length = MessageParser.calculate_dynamic_length(msg_type, stream)
                    except:
                        length = -1
            
            if length < 0 or stream.tell() + length > data_len:
                # 🚨 增加黑洞预警雷达！🚨
                print(f"\n🚨 [黑洞截断警告] 未知 OCG 指令 (Type {msg_type}) 缺失长度定义！")
                print(f"💥 强行吞噬了剩余的 {data_len - start_pos - 1} 个字节，这将导致后续时点丢失卡死！")
                print(f"📦 吞噬残骸 Hex: {data[start_pos:].hex()}")
                
                length = data_len - start_pos - 1
                
            payload = stream.read(length)
            msgs.append(bytes([msg_type]) + payload)
        return msgs

    async def handle_server_msg(self, msg_type, msg_data):
        try:
            if msg_type == 0x12: # STOC_JOIN_GAME
                print("✅ 成功加入房间！正在上传卡组...")
                await self.client.send_deck(self.deck.main, self.deck.extra)
                # 注意：这里先不要发 send_ready()，等服务器下发 0x13 之后再发
                
            elif msg_type == 0x13: # STOC_TYPE_CHANGE
                if len(msg_data) >= 1:
                    self.ai_player_id = msg_data[0] & 0x0F
                    # 👇 新增这一行：把真实座位号同步进 AI 大脑！
                    self.ai.player_id = self.ai_player_id 
                    print(f"🪑 服务器分配座位: Player {self.ai_player_id}")
                
                # 拿到座位号后，立刻发送准备信号
                await self.client.send_ready()
                
            elif msg_type == 0x18: # STOC_TIME_LIMIT
                # 👑 完美的排毒心跳包：MDPro 协议的心跳回复是 0x15 (CTOS_TIME_CONFIRM)
                await self.client.send_packet(0x15) 
                
            elif msg_type == 0x02: # STOC_ERROR_MSG
                err_type = msg_data[0]
                if err_type == 2 and len(msg_data) >= 5:
                    err_code = struct.unpack('<I', msg_data[1:5])[0]
                    print(f"\n🚨 [卡组被拒] 违规卡片 Code: {err_code}\n")
                    
            # 决斗开始 (0x15)，大厅退人 (0x14)
            elif msg_type in [0x15, 0x14] and len(msg_data) == 0: 
                print("⚔️ 决斗房间已锁定！进入战前准备阶段。")
                self.gamestate.reset()
                self.ignore_actions_blacklist = []

            elif msg_type == 0x03 and len(msg_data) == 0:
                import random
                await self.client.send_packet(0x03, bytes([random.choice([1, 2, 3])])) 

            elif msg_type == 0x05: # STOC_SELECT_TP
                print(f"👑 猜拳获胜！AI 按偏好选择: {'后攻' if self.prefer_second else '先攻'}...")
                # 0 代表自己先攻，1 代表对方先攻
                tp_choice = 1 if self.prefer_second else 0 
                await self.client.send_packet(0x04, bytes([tp_choice]))
            
            elif msg_type == 0x0C: # STOC_TYPE_CHANGE
                # 服务器下发座位号 (0 是 P1, 1 是 P2, 7 是观战)
                self.ai_player_id = msg_data[0] & 0x0F
                print(f"🪑 服务器分配座位: Player {self.ai_player_id}")

            elif msg_type == 0x01: 
                msgs = self.robust_parse(msg_data)
                
                for msg in msgs:
                    ocg_type = msg[0]
                    ocg_payload = msg[1:]
                    
                    # === 修复 1：完美处理 RETRY 死锁 ===
                    if ocg_type == 1: 
                        print(f"⚠️ [引擎警告] 操作不合法，触发重试 (MSG_RETRY)")
                        if hasattr(self, 'last_decision') and self.last_decision is not None:
                            self.ignore_actions_blacklist.append(self.last_decision)
                        
                        # 引擎在等你，必须立刻重新发起决策！
                        if self.gamestate.current_valid_actions and hasattr(self, 'last_prompt_type'):
                            print("🔄 正在触发重试决策...")
                            await self._make_decision(self.last_prompt_type, msg)
                        continue 
                    
                    # 只有非重试指令才重置黑名单（你原来的代码已有，保持即可）
                    if ocg_type in [10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 22, 23, 24, 25, 26, 140, 141, 142, 143]:
                         if ocg_type != 1: 
                             self.ignore_actions_blacklist = []
                             
                    self.gamestate.update(ocg_type, ocg_payload)
                    
                    # === 修复 2：增加座位隔离 (防抢答) ===
                    if self.gamestate.current_valid_actions:
                        # 只有引擎呼叫的 active_player 是 AI 自己时，才响应！
                        if self.ai_player_id is not None and self.gamestate.active_player == self.ai_player_id:
                            self.last_prompt_type = ocg_type # 记录当前提示类型，供 Retry 使用
                            await self._make_decision(ocg_type, msg)
                        else:
                            # 🎯 新增：明确打印出 AI 正在等待谁的操作
                            print(f"⏳ [时点移交] 引擎正在等待 Player {self.gamestate.active_player} (对方) 操作，AI 待机中...")
                            self.gamestate.current_valid_actions = []

        except Exception as e:
            print(f"❌ 消息处理崩溃: {e}")
            traceback.print_exc()

    async def _make_decision(self, msg_type, raw_msg):
        snap = self.gamestate.get_snapshot() 
        sync_valid_actions(snap.valid_actions)
        
        print(f"\\n--- 🎯 引擎提示 Type {msg_type}，当前可用选项 [{len(snap.valid_actions)} 个] ---")
        for idx, act in enumerate(snap.valid_actions):
            code_str = f" (Code: {act.code})" if hasattr(act, 'code') and act.code else ""
            print(f"  [{idx}] 动作类型: {act.action_type}, YGO索引: {act.index}{code_str}, 描述: {act.desc_str}")
        print("---------------------------------------------------------")
        
        decision = None
        if not self.ignore_actions_blacklist and hasattr(self.ai.net, 'parameters'):
            try:
                 decision = self.ai.get_decision(self.gamestate, msg_type)
                 print(f"🧠 AI 神经网络运算结果 -> 原始返回: {decision}")
            except Exception as e:
                 print(f"🧠 AI 神经网络决策异常: {e}")
                 
        if decision is None:
            decision = get_rule_decision(0, msg_type, raw_msg, self.gamestate, self.ignore_actions_blacklist)
            print(f"⚙️ RuleBot 接管运算 Cosmic Result -> 原始返回: {decision}")
            
        self.last_decision = decision
        await self.client.send_decision(decision) 
        self.gamestate.current_valid_actions = []

if __name__ == "__main__":
    HOST = "127.0.0.1" 
    PORT = 7911        
    PASS = ""          
    DECK = "神秘白龙" # 自动支持新卡组

    link = GalateaLink(HOST, PORT, PASS, DECK)
    asyncio.run(link.start())