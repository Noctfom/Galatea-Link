# ==================================================================================
#  Galatea AI Bot (Index Logic Fix)
#  修复了导致死锁的索引映射问题
# ==================================================================================

import torch
import torch.nn as nn
import os
import random
import struct
from dataclasses import dataclass
from typing import Any
# 引入桥接后的 FeatureEncoder
try:
    from feature_encoder import GalateaEncoder as FeatureEncoder
except ImportError:
    # 兼容旧代码或测试环境
    from galatea_net import FeatureEncoder 

from galatea_net import GalateaNet


@dataclass(frozen=True)
class CoreDecision:
    choice_id: int
    action: Any
    response: Any
    confidence: float
    probability_margin: float

class AiBot:
    def __init__(self, device='cpu', net_config=None):
        if net_config is None:
            net_config = {'d_model': 256, 'n_heads': 4, 'n_layers': 2, 'vocab_size': 20000}
            
        self.net = GalateaNet(net_config).to(device)
        self.device = device
        self.encoder = FeatureEncoder()
        self.net.eval() # 默认推理模式

    def load_model(self, path):
        if not os.path.exists(path):
            print(f"⚠️ 模型文件不存在: {path}")
            return False
        
        try:
            checkpoint = torch.load(path, map_location=self.device)
            
            # [新逻辑] 检查是否包含配置字典
            if isinstance(checkpoint, dict) and 'net_config' in checkpoint:
                saved_config = checkpoint['net_config']
                print(f"📦 发现内嵌配置: {saved_config}")
                self.net = GalateaNet(saved_config).to(self.device)
                self.net.load_state_dict(checkpoint['model_state_dict'])
                self.net.eval()
                print(f"✅ 网络已自动重构并加载权重。")
                return True
            
            # [旧逻辑]
            elif isinstance(checkpoint, dict) and 'model_state_dict' in checkpoint:
                 self.net.load_state_dict(checkpoint['model_state_dict'])
                 return True
            else:
                self.net.load_state_dict(checkpoint)
                return True

        except Exception as e:
            print(f"❌ 加载模型失败: {e}")
            return False

    def get_action_and_value_from_tensor(self, obs_dict, valid_actions_list=None):
        """
        [训练专用 - Action Head版] 获取动作概率和价值
        """
        # 1. 前向传播
        # logits: [B, MAX_ACTIONS] (已在网络内部Mask，无效动作是 -1e9)
        # value:  [B, 1]
        logits, value, v_input = self.net(obs_dict)
        
        # 2. 构建分布
        # Categorical 会自动对 logits 做 softmax
        # -1e9 的项概率会变成 0，不会被采样到
        dist = torch.distributions.Categorical(logits=logits)
        
        # 3. 采样
        action = dist.sample()
        
        # 返回: action(索引), log_prob, entropy(平均值), value
        return action, dist.log_prob(action), dist.entropy().mean(), value, v_input

    def get_decision(self, gamestate, msg_type, msg_args=None):
        snap = gamestate.get_snapshot(self.env)
        return self.get_decision_from_snapshot(snap, msg_type, msg_args)

    # 根据独立快照计算动作以避免后台线程读取可变对局状态
    def get_decision_from_snapshot(self, snap, msg_type, msg_args=None):
        decision = self.get_scored_decision_from_snapshot(snap, msg_type, msg_args)
        return decision.response if decision is not None else None

    # 计算 Core 动作及用于介入策略的概率信息
    def get_scored_decision_from_snapshot(self, snap, msg_type, msg_args=None):
        self.net.eval()
        if not snap.valid_actions or not snap.entities:
            return None

        tensor_dict = self.encoder.encode(snap, player_id=snap.global_data.to_play)
        
        with torch.no_grad():
            gpu_dict = {k: v.to(self.device) for k, v in tensor_dict.items()}
            
            # Logits 现在直接就是 [1, 80] 的动作分数
            logits, value, _ = self.net(gpu_dict) 
            
            # 网络已经内置了 act_mask 并把无效槽位变成了 -1e9
            # 不需要手动切片，直接 Argmax，不可能选到 Padding
            valid_logits = logits[0][:len(snap.valid_actions)]
            probabilities = torch.softmax(valid_logits, dim=-1)
            sel_idx = torch.argmax(probabilities).item()
            confidence = float(probabilities[sel_idx].item())
            if len(snap.valid_actions) > 1:
                top_two = torch.topk(probabilities, k=2).values
                probability_margin = float((top_two[0] - top_two[1]).item())
            else:
                probability_margin = 1.0

        chosen = snap.valid_actions[sel_idx]
        response = self._pack_response(chosen, msg_type, msg_args)
        return CoreDecision(
            choice_id=sel_idx,
            action=chosen,
            response=response,
            confidence=confidence,
            probability_margin=probability_margin,
        )

    # 将 LLM 返回的合法 choice_id 转换为游戏协议响应
    def pack_choice_from_snapshot(self, snap, choice_id, msg_type, msg_args=None):
        if isinstance(choice_id, bool) or not isinstance(choice_id, int):
            raise ValueError("choice_id 必须是整数")
        if not 0 <= choice_id < len(snap.valid_actions):
            raise ValueError(f"choice_id 超出合法动作范围: {choice_id}")
        return self._pack_response(snap.valid_actions[choice_id], msg_type, msg_args)

    def _pack_response(self, action, msg_type=0, msg_args=None):
        # ==========================================================
        # 优先检查动作是否携带有物理外挂（宏动作包裹）
        # 如果有 decision_bytes，说明这是经过 RuleBot 完美打包的套餐，直接透传
        # ==========================================================
        if hasattr(action, 'decision_bytes') and action.decision_bytes:
            return action.decision_bytes
        # ==========================================================
        # 1. 整型槽类 (调用 C++ set_responsei) - 绝对不能返回 bytes
        # 包含: 10(Battle), 11(Idle), 12(EffectYN), 13(YesNo), 
        #       14(Option), 16(Chain), 140~143(各类宣言)
        # ==========================================================
        if msg_type in [10, 11, 12, 13, 14, 16, 140, 141, 142, 143]:
            if msg_type in [10, 11]:
                return int((action.index << 16) | action.action_type)
            elif msg_type in [140, 141, 142]:
                return int(action.desc_id)
            else:
                return int(action.index)

        # ==========================================================
        # 2. 字节槽类 (调用 C++ set_responseb) - 必须带 count 字节
        # 包含: 15(SelectCard), 20(Tribute), 22(Counter), 26(Unselect)
        # ==========================================================
        elif msg_type in [15, 20, 22, 26]:
            if action.index < 0 or action.index > 255:
                # Cancel 指令 (-1)，转换为 4 字节的 0xFFFFFFFF
                return int(-1).to_bytes(4, byteorder='little', signed=True)
            # 兜底：数量(Count)=1, 后接选中的索引
            return bytes([1, action.index]) 
        
        # ==========================================================
        # 3. 物理格子类 (Place / Disfield) - 严格的 3 字节
        # ==========================================================
        elif msg_type in [18, 24]:
            zone_id = action.index
            
            # 🎯 获取 AI 真实的绝对座位号（默认 0 兜底）
            my_p = getattr(self, 'player_id', 0) 
            op_p = 1 - my_p
            
            # 如果索引大于等于 16，说明是对手的格子；否则是自己的格子
            p = op_p if (zone_id & 16) else my_p
            l = 0x08 if (zone_id & 8) else 0x04
            s = zone_id & 7
            
            return bytes([p, l, s])
            
        # 兜底防护：所有未知指令全部返回整数，防止字节野指针
        return int(action.index)
