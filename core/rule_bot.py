'''
规则型 Bot 的核心决策逻辑
按实际提示生成响应，无法满足已知约束时明确报告错误
'''


import struct
import io
import random
import itertools
from game_constants import LocationInfo
from utils.card_reader import card_db
from core.selection_options import build_selection_options
from core.selection_protocol import SELECTION_MSGS, SelectionProtocolError
from core.response_codec import encode_int_response

# --- 消息类型常量 ---
MSG_SELECT_BATTLECMD = 10
MSG_SELECT_IDLECMD = 11
MSG_SELECT_EFFECTYN = 12
MSG_SELECT_YESNO = 13
MSG_SELECT_OPTION = 14
MSG_SELECT_CARD = 15
MSG_SELECT_CHAIN = 16
MSG_SELECT_PLACE = 18
MSG_SELECT_POSITION = 19
MSG_SELECT_TRIBUTE = 20
MSG_SELECT_COUNTER = 22
MSG_SELECT_SUM = 23
MSG_SELECT_DISFIELD = 24
MSG_SORT_CARD = 25
MSG_SELECT_UNSELECT_CARD = 26
MSG_ANNOUNCE_RACE = 140
MSG_ANNOUNCE_ATTRIB = 141
MSG_ANNOUNCE_CARD = 142
MSG_ANNOUNCE_NUMBER = 143

# --- 常量定义 ---
POS_FACEUP_ATTACK = 0x1
POS_FACEDOWN_ATTACK = 0x2
POS_FACEUP_DEFENSE = 0x4
POS_FACEDOWN_DEFENSE = 0x8

# --- 辅助解析函数 ---

_shared_valid_actions = []

def sync_valid_actions(actions):
    """接收来自 gamestate 完美解析的合法动作池"""
    global _shared_valid_actions
    _shared_valid_actions = actions

# --- 辅助算法: Subset Sum ---
def solve_subset_sum(target_val, candidates, min_c, max_c):
    """
    寻找组合，使数值之和等于 target_val
    """
    # 尝试从 min_c 到 max_c 的所有数量组合
    for r in range(min_c, max_c + 1):
        for combination in itertools.combinations(candidates, r):
            current_sum = sum(c['val'] for c in combination)
            if current_sum == target_val:
                return [c['index'] for c in combination]
    return None

def parse_idle_cmd(msg_data):
    """解析 IDLE 消息，提取所有合法动作"""
    stream = io.BytesIO(msg_data)
    stream.read(1) # player
    legal_actions = []
    
    # 0:Summon, 1:SpSummon, 2:Repos, 3:MSet, 4:SSet, 5:Activate
    for cat in range(6):
        b = stream.read(1)
        if not b: break
        count = struct.unpack('B', b)[0]
        
        for i in range(count):
            stream.read(7) # code(4) + con(1) + loc(1) + seq(1)
            if cat == 5: stream.read(4) # desc
            legal_actions.append({'cat': cat, 'idx': i})          

    b = stream.read(1)
    if b and struct.unpack('B', b)[0]: legal_actions.append({'cat': 6, 'idx': 0}) # BP
    b = stream.read(1)
    if b and struct.unpack('B', b)[0]: legal_actions.append({'cat': 7, 'idx': 0}) # EP
    return legal_actions

def parse_battle_cmd(msg_data):
    """解析 BATTLE 消息"""
    stream = io.BytesIO(msg_data)
    stream.read(1) # player
    legal_actions = []
    
    # Activatable (发动效果)
    b = stream.read(1) 
    if b:
        count = struct.unpack('B', b)[0]
        for i in range(count): 
            stream.read(11) # Skip details
            legal_actions.append({'cat': 0, 'idx': i})
            
    # Attackable
    b = stream.read(1) 
    if b:
        count = struct.unpack('B', b)[0]
        for i in range(count):
            code = struct.unpack('<I', stream.read(4))[0]
            stream.read(4) 
            legal_actions.append({'cat': 1, 'idx': i, 'code': code})
            
    # M2 / EP
    b = stream.read(1); 
    if b and struct.unpack('B', b)[0]: legal_actions.append({'cat': 2, 'idx': 0}) 
    b = stream.read(1); 
    if b and struct.unpack('B', b)[0]: legal_actions.append({'cat': 3, 'idx': 0}) 
    return legal_actions

# --- 核心决策逻辑 ---

def get_rule_decision(
    player_id,
    msg_type,
    msg,
    gamestate,
    ignore_actions=None,
    valid_actions=None,
):
    """
    按已知网络合同处理请求，复杂选择无可用合法响应时明确报错
    """
    if ignore_actions is None: ignore_actions = []
    action_pool = _shared_valid_actions if valid_actions is None else valid_actions
    payload = msg[1:] # 去掉 msg_type 头
    if msg_type in SELECTION_MSGS:
        ignored = set()
        for response in ignore_actions:
            if isinstance(response, (bytes, bytearray)):
                ignored.add(bytes(response))
            elif isinstance(response, int):
                ignored.add(encode_int_response(response))
        options = build_selection_options(msg_type, payload, gamestate)
        available = [option["bytes"] for option in options if option["bytes"] not in ignored]
        if not available:
            raise SelectionProtocolError("当前复杂选择没有未被拒绝的合法响应")
        return random.choice(available)
    stream = io.BytesIO(payload)
    
    decision = None # 最终决策结果，用于返回和记录

    try:
        # ==================== 1. 基础战斗/闲置逻辑 ====================
        # =================================================================
        # [终极修复] 7. 闲置命令 (Idle)：字符串 Key 过滤法
        # =================================================================
        if msg_type == MSG_SELECT_IDLECMD:
            actions = parse_idle_cmd(payload)
            valid_actions = []
            
            # 1. 构建黑名单 Key 集合
            ignored_keys = set()
            for val in ignore_actions:
                if isinstance(val, int):
                    i_cat = val & 0xFFFF
                    i_idx = (val >> 16) & 0xFFFF
                    ignored_keys.add(f"{i_cat}:{i_idx}")
                elif isinstance(val, str):
                    ignored_keys.add(val)

            # 2. 筛选合法操作
            for a in actions:
                cat = a['cat']
                idx = a['idx']
                if f"{cat}:{idx}" not in ignored_keys:
                    resp = (idx << 16) | cat
                    valid_actions.append(resp)
            
            if valid_actions:
                decision = random.choice(valid_actions)
            else:
                # [关键改动] 绝境处理：所有操作都被拉黑了
                # 检查 "To EP" (Cat=7) 和 "To BP" (Cat=6) 是否在黑名单里
                
                cmd_ep = (0 << 16) | 7
                cmd_bp = (0 << 16) | 6
                
                # 如果 EP 没被拉黑，且 actions 里包含 EP (虽然被上面的 key 过滤了，但我们这里是强制尝试)
                # 实际上 parse_idle_cmd 会解析出是否有 EP/BP 选项。
                # 简单粗暴点：
                
                if f"7:0" not in ignored_keys:
                    decision = cmd_ep # 尝试结束回合
                elif f"6:0" not in ignored_keys:
                    decision = cmd_bp # 尝试进战阶
                else:
                    # EP 和 BP 都不行？那只能随便发一个“投降”或“空操作”来触发外部熔断了
                    # 这里发一个不存在的 Cat 10，让 Core 报错而不是卡逻辑
                    decision = (0 << 16) | 10

        elif msg_type == MSG_SELECT_BATTLECMD:
            actions = parse_battle_cmd(payload)
            valid_actions = []
            
            # 黑名单过滤
            for a in actions:
                if a['cat'] == 1: test_val = (a['idx'] << 16) | 1
                else: test_val = (0 << 16) | a['cat']
                if test_val not in ignore_actions:
                    valid_actions.append(a)

            if not valid_actions: 
                # 🌟 核心修复：不要硬编码，动态检查引擎到底允许进哪个阶段
                can_m2 = any(a['cat'] == 2 for a in actions)
                can_ep = any(a['cat'] == 3 for a in actions)
                
                # 优先尝试 M2，再尝试 EP，且避开被拉黑的选项
                if can_m2 and ((0 << 16) | 2) not in ignore_actions:
                    decision = (0 << 16) | 2
                elif can_ep and ((0 << 16) | 3) not in ignore_actions:
                    decision = (0 << 16) | 3
                else:
                    decision = (0 << 16) | 10 # 彻底卡死时的无奈之举，触发外界熔断
            else:
                choice = random.choice(valid_actions)
                if choice['cat'] in [0, 1]: 
                    decision = (choice['idx'] << 16) | choice['cat']
                else:
                    decision = (0 << 16) | choice['cat']

        # ==================== 2. 简单二选一/多选一 ====================
        # [修复] Type 13: 必须检查 ignore_actions
        elif msg_type in [MSG_SELECT_YESNO, MSG_SELECT_EFFECTYN]:
            candidates = [0, 1]
            valid = [x for x in candidates if x not in ignore_actions]
            if valid:
                decision = random.choice(valid)
            else:
                decision = random.choice([0, 1])

        # =================================================================
        # [修复] 4. 选项选择：排除错误选项
        # =================================================================
        # [修复] Type 14: 必须检查 ignore_actions 且不能返回死锁的 0
        elif msg_type == MSG_SELECT_OPTION:
            # [修复] 防空包崩溃
            if len(payload) < 2: return bytes([0])
            
            stream.read(1) 
            try:
                count = struct.unpack('B', stream.read(1))[0]
            except:
                return bytes([0])
            possible_choices = list(range(count))
            
            valid_choices = [i for i in possible_choices if i not in ignore_actions]
            
            if valid_choices:
                decision = random.choice(valid_choices)
            else:
                decision = -1 # 尝试取消

        elif msg_type == MSG_SELECT_CHAIN:
            try:
                # 1. 精确读取头部的 4 个字节
                header_start = stream.read(4)
                if len(header_start) < 4: raise Exception("Header incomplete")
                
                count = header_start[1]
                forced = header_start[3] # 强制标志位
                
                # 2. 构造选项：无论是谁，只能从 0 到 count-1 里选
                candidates = list(range(count))
                
                # 如果不是强制发动 (forced == 0)，才可以追加 Cancel (-1)
                if forced == 0:
                    candidates.append(-1)
                    
                # 3. 过滤掉已经被核心拒绝过的操作
                valid_choices = [c for c in candidates if c not in ignore_actions]
                
                # 4. 安全决策
                if valid_choices:
                    decision = random.choice(valid_choices)
                else:
                    # 终极兜底：如果所有合法选项都被拒绝了，说明 C++ 陷入了必须发动的死角
                    # 我们强行返回 0（发动第一个效果），绝不返回 -1
                    decision = 0 
                    
            except Exception:
                decision = 0
            
            return decision

        # ==================== 3. 宣言与竞猜 (新增与补全) ====================
        # 这类消息如果不处理，遇到《抹杀之指名者》等卡会直接卡死
        
        # =================================================================
        # [终极修正] 10. 种族/属性宣言 (位掩码处理)
        # =================================================================
        elif msg_type in [MSG_ANNOUNCE_RACE, MSG_ANNOUNCE_ATTRIB]:
            stream.read(1) # Player
            count = struct.unpack('B', stream.read(1))[0] # 需要选几个
            available = struct.unpack('<I', stream.read(4))[0] # 可选掩码
            
            # 1. 解析掩码，找出所有可用的位 (Races/Attributes)
            options = []
            for i in range(32): # 遍历 32 位
                bit = 1 << i
                if available & bit:
                    options.append(bit)
            
            # 2. 随机选择 count 个
            # 如果 options 不够选，就全选
            if len(options) <= count:
                selected = options
            else:
                selected = random.sample(options, count)
            
            # 3. 计算结果掩码 (求和/按位或)
            result_mask = 0
            for bit in selected:
                result_mask |= bit
            
            # 4. 发送 4 字节整数
            decision = struct.pack('<I', result_mask)

        # =================================================================
        #  卡片/数字宣言 
        # =================================================================
        elif msg_type == MSG_ANNOUNCE_CARD: # 142: 必须返回真实卡密
            stream.read(1)
            count = struct.unpack('B', stream.read(1))[0]
            for _ in range(count): stream.read(4)
            
            # 彻底抛弃原来愚蠢的“盲猜灰流丽”逻辑
            # 直接从 gamestate 传过来的完美选项池里提取合法的卡密
            valid_codes = [act.desc_id for act in action_pool if act.action_type == 142]
            
            # 过滤掉已经被引擎拒绝的卡（防死锁保护）
            safe_codes = []
            for code in valid_codes:
                packed = struct.pack('<I', code)
                if packed not in ignore_actions:
                    safe_codes.append(packed)
            
            if safe_codes:
                # 完美过关
                decision = random.choice(safe_codes)
            else:
                print("🚨 [RuleBot 警报] Type 142 (宣言卡片) 没有合法选项可选！")

        elif msg_type == MSG_ANNOUNCE_NUMBER: # 143: 必须返回索引
            stream.read(1)
            count = struct.unpack('B', stream.read(1))[0]
            for _ in range(count): stream.read(4) 
            
            ignored_set = set(b for b in ignore_actions if isinstance(b, bytes))
            decision = struct.pack('<I', 0)
            
            if count > 0:
                valid_indices = []
                for i in range(count):
                    cand = struct.pack('<I', i)
                    if cand not in ignored_set: valid_indices.append(cand)
                if valid_indices:
                    decision = random.choice(valid_indices)

        # ==================== 4. 复杂对象选择 (位置/卡片) ====================
        
        # =================================================================
        # [修复] 5. 表示形式选择：修正解析偏移 + 移除非法选项
        # =================================================================
        # [修复] Type 19: 必须使用 valid_ops 而不是 options
        elif msg_type == 19: # MSG_SELECT_POSITION
            try:
                stream.read(1); stream.read(4)
                mask_byte = stream.read(1)
                if not mask_byte: decision = bytes([1])
                else:
                    mask = struct.unpack('B', mask_byte)[0]
                    options = []
                    if mask & 0x1: options.append(1)
                    if mask & 0x2: options.append(2)
                    if mask & 0x4: options.append(4)
                    if mask & 0x8: options.append(8)
                    
                    # 过滤黑名单
                    valid_ops = [o for o in options if o not in ignore_actions and bytes([o]) not in ignore_actions]
                    
                    if valid_ops:
                        # [关键] 从 valid_ops 选
                        decision = bytes([random.choice(valid_ops)])
                    elif options:
                        # 绝境：随机盲选
                        decision = bytes([random.choice(options)])
                    else:
                        decision = bytes([1])
            except Exception as e:
                print(f"⚠️ [RuleBot] 处理 MSG_SELECT_POSITION 时发生异常: {e}")
                decision = bytes([1])

        # [终极修正] 6. 全局位置选择 (Place/Disfield)
        # =================================================================
        # [RuleBot 核弹级修复] 6. 全局位置选择 (Place/Disfield)
        elif msg_type in [MSG_SELECT_PLACE, MSG_SELECT_DISFIELD]:
            stream.seek(0) 
            req_player = struct.unpack('B', stream.read(1))[0]
            count = struct.unpack('B', stream.read(1))[0]
            # 防止核心发疯传来 count=0 导致 b'' 和 loc=0 越界崩溃
            count = max(1, count)
            mask = struct.unpack('<I', stream.read(4))[0]
            
            # 1. 黑名单强力清洗：只保留长度为 3 的 bytes
            safe_ignore_set = set()
            for x in ignore_actions:
                if isinstance(x, (bytes, bytearray)) and len(x) == 3:
                    safe_ignore_set.add(bytes(x))
            
            # 2. 合法位置生成 (确保绝对是 3 字节)
            valid_locs = []
            for i in range(32):
                if not (mask & (1 << i)):
                    p = 0
                    l = 0x04
                    s = 0
                    if i & 16: p = 1
                    if i & 8:  l = 0x08
                    s = i & 0x7 
                    
                    if l == 0x04 and s > 6: continue
                    if l == 0x08 and s > 7: continue 
                    
                    # === 修复开始 ===
                    # 原始逻辑: target_p = req_player if p == 0 else (1 - req_player)
                    # 问题: 如果 req_player > 1，(1-req) 会变成负数，导致 bytes() 崩溃
                    
                    # 修复逻辑: 无论算出什么，强制取模或限制在 0-1
                    raw_p = req_player if p == 0 else (1 - req_player)
                    target_p = 1 if raw_p == 1 else 0 # 任何非1的值都变成0，防止负数
                    loc_bytes = bytes([target_p, l, s]) # 绝对是 3 字节
                    valid_locs.append(loc_bytes)

            # 3. 决策生成
            candidates = []
            if count == 1:
                for loc in valid_locs:
                    # 构造完整决策包 (单选时就是 loc 本身)
                    # 检查是否在黑名单
                    if loc not in safe_ignore_set:
                        candidates.append(loc)
                
                # 绝境回退：如果全被拉黑，尝试随机选一个合法的（撞大运）
                if not candidates and valid_locs:
                    candidates = valid_locs
                    print(f"🚨 [RuleBot 警报] Type {msg_type} (站位/封锁) 合法位置全被拉黑！")
                    print(f"   -> 引擎提供可用位置: {valid_locs}")
                    print(f"   -> 当前黑名单: {safe_ignore_set}")
            else:
                candidates = valid_locs # 多选暂不过滤

            resp_buf = bytearray()
            
            if candidates:
                random.shuffle(candidates)
                # 确保取出的数量不超过 count
                # 如果 candidates 不够，就取全部，后面补 0
                selected = candidates[:count]
                
                for loc in selected:
                    resp_buf.extend(loc)
            
            # 4. 最终守门员：强制补齐与校验
            expected_len = max(1, count) * 3
            
            # 如果长度不够，补 0
            while len(resp_buf) < expected_len:
                resp_buf.extend([0, 0, 0])
            
            # 如果长度超了（理论不应发生），截断
            if len(resp_buf) > expected_len:
                resp_buf = resp_buf[:expected_len]
                
            decision = bytes(resp_buf)
            
            # [双重保险] 如果 decision 居然还是 2 (比如被某些诡异逻辑覆盖了)
            # 这里做最后的类型检查
            if not isinstance(decision, bytes) or len(decision) != expected_len:
                decision = bytes([0] * expected_len)

        # =================================================================
        # [新增] 12. 猜拳与手牌/先攻选择 (Type 132, 133)
        # =================================================================
        elif msg_type == 132: # MSG_ROCK_PAPER_SCISSORS
            stream.read(1) # Player
            decision = random.choice([1, 2, 3]) # 1:剪刀, 2:石头, 3:布

        elif msg_type == 133: # MSG_HAND_RES (选先攻/后攻)
            stream.read(1) # Player
            # 1: 先攻, 2: 后攻 (通常)
            decision = random.choice([1, 2])

        # =================================================================
        # [新增] 13. 硬币与骰子 (Type 130, 131)
        # =================================================================
        elif msg_type == 130: # MSG_TOSS_COIN
            stream.read(1) # Player
            count = struct.unpack('B', stream.read(1))[0]
            # 这里的逻辑通常是 Core 告诉客户端结果，或者是客户端确认
            # 如果需要回复，通常是发 0 或 1 (猜正反)
            # 简单起见，发 0 (Heads) 或 1 (Tails) * Count
            resp_buf = bytearray([random.choice([0, 1]) for _ in range(count)])
            decision = bytes(resp_buf)

        elif msg_type == 131: # MSG_TOSS_DICE
            stream.read(1) # Player
            count = struct.unpack('B', stream.read(1))[0]
            # 同上，回复占位符
            resp_buf = bytearray([0] * count)
            decision = bytes(resp_buf)
            
    except Exception as e:
        # 万一解析崩了，也要保证返回合法格式，避免 Bot 卡死
        print(f"[RuleBot] 解析出现错误 {msg_type}: {e}")
        decision = -1

    if decision is None:
        print(f"🚨 [RuleBot 警报] 消息类型 {msg_type} 的解析逻辑彻底失效，没有任何分支返回 decision！")
        print(f"   -> Payload 长度: {len(payload)}")
        decision = -1
    
    return decision


def get_macro_options(msg_type, msg_payload, brain, limit=5000, pref_weights=None):
    """
    [AI 参谋部] 后台穷举合法素材组合，打包成“套餐”供 AI 挑选
    返回格式: [{'bytes': b'\x01...', 'locs': [loc_raw1, loc_raw2]}, ...]
    """
    if pref_weights is None: pref_weights = {}
    if msg_type in SELECTION_MSGS:
        return build_selection_options(msg_type, msg_payload, brain, limit=limit, pref_weights=pref_weights)
    stream = io.BytesIO(msg_payload)
    options = []

    # 1. 抓取犯罪嫌疑卡 (用于雷达日志溯源)
    trigger_card = "未知机制/阶段动作"
    if brain and brain.chain_stack:
        trigger_card = f"【{card_db.get_card_name(brain.chain_stack[-1]['code'])}】"
    elif brain and brain.history_stack:
        trigger_card = f"【{card_db.get_card_name(brain.history_stack[0]['code'])}】"
    
    try:
        # 位置及宣言继续使用独立的网络布局
        if msg_type in [18, 24]:
            stream.read(1) # P
            count = struct.unpack('B', stream.read(1))[0]
            mask = struct.unpack('<I', stream.read(4))[0]
            req_player = msg_payload[0] 
            
            if count == 0:
                options.append({'bytes': bytes([0, 0, 0]), 'places': []})
            
            avail_zones = []
            for i in range(32):
                if not (mask & (1 << i)): avail_zones.append(i)
            
            calc_count = max(1, count)
            all_combos = []
            for i, combo in enumerate(itertools.combinations(avail_zones, calc_count)):
                all_combos.append(combo)
                if i >= limit - 1:
                    print(f"📡 [RuleBot 截断雷达] Type {msg_type} (格子选择) 达到上限阻断。")
                    print(f"   -> 🎯 发动源头: {trigger_card}")
                    print(f"   -> 📊 可用空余格子数: {len(avail_zones)}, 需选: {calc_count}")
                    break
            
            for combo in all_combos:
                resp_buf = bytearray()
                places = []
                for i in combo:
                    p_flag = 1 if i >= 16 else 0
                    l = 0x08 if (i % 16) >= 8 else 0x04
                    s = i % 8
                    raw_p = req_player if p_flag == 0 else (1 - req_player)
                    final_p = 1 if raw_p == 1 else 0
                    resp_buf.extend([final_p, l, s])
                    places.append(i)
                options.append({'bytes': bytes(resp_buf), 'places': places})

        # 6. 种族与属性宣言生成完整位掩码组合
        elif msg_type in [MSG_ANNOUNCE_RACE, MSG_ANNOUNCE_ATTRIB]:
            stream.read(1) # P
            required_count = struct.unpack('B', stream.read(1))[0]
            available_mask = struct.unpack('<I', stream.read(4))[0]
            available_bits = [
                bit_index
                for bit_index in range(32)
                if available_mask & (1 << bit_index)
            ]
            for option_index, bit_indices in enumerate(
                itertools.combinations(available_bits, required_count)
            ):
                if option_index >= limit:
                    break
                response_value = 0
                for bit_index in bit_indices:
                    response_value |= 1 << bit_index
                options.append({
                    'value': response_value,
                    'indices': list(bit_indices),
                    'values': [bit_index + 1 for bit_index in bit_indices],
                })
                
    except Exception as e:
        print(f"⚠️ [参谋部] get_macro_options 解析错误: {e}")
        import traceback
        traceback.print_exc()
        
    return options
