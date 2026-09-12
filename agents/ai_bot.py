# ==================================================================================
#  Galatea AI Bot (Index Logic Fix)
#  修复了导致死锁的索引映射问题
# ==================================================================================

import numpy as np
import os
import random
import struct
import sys
from dataclasses import dataclass
from typing import Any
from model_protocols import load_model_backend


# 安全输出模型日志并兼容 Windows 控制台编码
def _console_print(message):
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        safe_message = message.encode(encoding, errors='replace').decode(encoding)
        print(safe_message)


@dataclass(frozen=True)
class CoreDecision:
    """保存 Core 动作及其介入策略概率信息"""

    choice_id: int
    action: Any
    response: Any
    confidence: float
    probability_margin: float
    policy_mode: str = "greedy"
    temperature: float | None = None

class AiBot:
    def __init__(self, device='cpu', net_config=None, initialize_network=True):
        """初始化 AI 控制器并允许延迟构建推理网络"""
        if net_config is None:
            net_config = {'d_model': 256, 'n_heads': 4, 'n_layers': 2, 'vocab_size': 20000}

        self.net = None
        self.inference_runtime = None
        self.device = device
        self.encoder = None
        self.model_metadata = None
        self.adapter_id = "core-3.4.2-model-v1"
        self.model_protocol_version = 1
        self.max_actions = 80
        self._rng = np.random.default_rng()
        if initialize_network:
            from feature_encoder import GalateaEncoder as FeatureEncoder
            from galatea_net import GalateaNet
            from model_protocols.inference_runtime import PyTorchInferenceRuntime

            self.net = GalateaNet(net_config).to(device)
            self.net.eval() # 默认推理模式
            self.encoder = FeatureEncoder()
            self.inference_runtime = PyTorchInferenceRuntime(self.net, device)

    # 自动识别并严格加载已注册的 Core 模型协议
    def load_model(
        self,
        path,
        expected_model_id=None,
        *,
        protocol="auto",
        asset_path=None,
        strict_asset_hashes=True,
        inference_backend="auto",
        onnx_providers=("CPUExecutionProvider",),
        onnx_intra_op_threads=0,
    ):
        if not os.path.exists(path):
            _console_print(f"⚠️ 模型文件不存在: {path}")
            self.net = None
            self.inference_runtime = None
            return False

        try:
            backend = load_model_backend(
                path,
                device=self.device,
                expected_model_id=expected_model_id,
                protocol=protocol,
                asset_path=asset_path,
                strict_asset_hashes=strict_asset_hashes,
                inference_backend=inference_backend,
                onnx_providers=tuple(onnx_providers),
                onnx_intra_op_threads=onnx_intra_op_threads,
            )
            self.net = backend.net
            self.inference_runtime = backend.inference_runtime
            self.encoder = backend.encoder
            self.model_metadata = backend.metadata
            self.adapter_id = backend.adapter_id
            self.model_protocol_version = backend.metadata["model_protocol_version"]
            self.max_actions = backend.max_actions
            _console_print(
                "✅ Core 模型已按版本适配器加载 "
                f"adapter={backend.adapter_id} "
                f"model_id={backend.metadata['model_id']} "
                f"iteration={backend.metadata['iteration']} "
                f"runtime={backend.runtime_id}"
            )
            return True

        except Exception as e:
            _console_print(f"❌ 加载模型失败: {e}")
            self.net = None
            self.inference_runtime = None
            self.model_metadata = None
            return False

    # 返回本地 Core 模型是否可以安全参与决策
    @property
    def model_available(self):
        return (
            getattr(self, "inference_runtime", None) is not None
            or getattr(self, "net", None) is not None
        )

    # 通过统一运行时执行推理并兼容旧测试网络
    def _infer(self, observation):
        runtime = getattr(self, "inference_runtime", None)
        if runtime is not None:
            return runtime.infer(observation)
        if self.net is None:
            raise RuntimeError("当前 AI 控制器未加载本地推理网络")
        import torch

        self.net.eval()
        with torch.no_grad():
            device_inputs = {
                key: value.to(self.device)
                for key, value in observation.items()
            }
            logits, values, value_input = self.net(device_inputs)
        return (
            logits.detach().cpu().numpy(),
            values.detach().cpu().numpy(),
            value_input,
        )

    # 计算数值稳定的单批次 softmax 概率
    @staticmethod
    def _softmax(values):
        scores = np.asarray(values, dtype=np.float64)
        scores = scores - np.max(scores)
        exponentials = np.exp(scores)
        total = exponentials.sum()
        if not np.isfinite(total) or total <= 0:
            raise RuntimeError("模型动作分数无法转换为有效概率")
        return exponentials / total

    def get_action_and_value_from_tensor(self, obs_dict, valid_actions_list=None):
        """
        [训练专用 - Action Head版] 获取动作概率和价值
        """
        if self.net is None:
            raise RuntimeError("当前 AI 控制器未加载本地推理网络")
        import torch

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
        if not self.model_available:
            raise RuntimeError("当前 AI 控制器未加载本地推理网络")
        snap = gamestate.get_snapshot(self.env)
        return self.get_decision_from_snapshot(snap, msg_type, msg_args)

    # 根据独立快照计算动作以避免后台线程读取可变对局状态
    def get_decision_from_snapshot(self, snap, msg_type, msg_args=None):
        decision = self.get_scored_decision_from_snapshot(snap, msg_type, msg_args)
        return decision.response if decision is not None else None

    # 计算 Core 动作及用于介入策略的概率信息
    def get_scored_decision_from_snapshot(
        self,
        snap,
        msg_type,
        msg_args=None,
        *,
        policy_mode="greedy",
        temperature=0.8,
    ):
        """按所选 Core 策略返回动作、置信度和协议响应"""
        if not self.model_available:
            raise RuntimeError("当前 AI 控制器未加载本地推理网络")
        if self.net is not None:
            self.net.eval()
        if not snap.valid_actions or not snap.entities:
            return None

        runtime = getattr(self, "inference_runtime", None)
        output_format = (
            "numpy"
            if getattr(runtime, "runtime_id", None) == "onnxruntime"
            else "torch"
        )
        try:
            tensor_dict = self.encoder.encode(
                snap,
                player_id=snap.global_data.to_play,
                output_format=output_format,
            )
        except TypeError:
            tensor_dict = self.encoder.encode(
                snap,
                player_id=snap.global_data.to_play,
            )
        logits, _, _ = self._infer(tensor_dict)

        # 网络已经内置 act_mask 并把无效槽位变成极小值
        valid_logits = np.asarray(logits)[0, :len(snap.valid_actions)]
        normalized_policy = str(policy_mode).strip().casefold()
        if normalized_policy not in {"greedy", "deployment"}:
            raise ValueError(f"不支持的 Core 动作策略: {policy_mode}")
        normalized_temperature = float(temperature)
        if not 0.05 <= normalized_temperature <= 5.0:
            raise ValueError("Core 模型温度必须位于 0.05 到 5.0")
        if normalized_policy == "deployment":
            probabilities = self._softmax(valid_logits / normalized_temperature)
            rng = getattr(self, "_rng", None)
            if rng is None:
                rng = np.random.default_rng()
                self._rng = rng
            sel_idx = int(rng.choice(len(probabilities), p=probabilities))
        else:
            probabilities = self._softmax(valid_logits)
            sel_idx = int(np.argmax(probabilities))
        confidence = float(probabilities[sel_idx])
        if len(snap.valid_actions) > 1:
            top_two = np.partition(probabilities, -2)[-2:]
            probability_margin = float(top_two.max() - top_two.min())
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
            policy_mode=normalized_policy,
            temperature=(
                normalized_temperature
                if normalized_policy == "deployment"
                else None
            ),
        )

    # 计算基础动作概率供复杂宏动作候选池进行两阶段筛选
    def get_action_probabilities_from_snapshot(self, snap):
        if not self.model_available:
            raise RuntimeError("当前 AI 控制器未加载本地推理网络")
        if not snap.valid_actions or not snap.entities:
            return []

        if self.net is not None:
            self.net.eval()
        runtime = getattr(self, "inference_runtime", None)
        output_format = (
            "numpy"
            if getattr(runtime, "runtime_id", None) == "onnxruntime"
            else "torch"
        )
        try:
            tensor_dict = self.encoder.encode(
                snap,
                player_id=snap.global_data.to_play,
                output_format=output_format,
            )
        except TypeError:
            tensor_dict = self.encoder.encode(
                snap,
                player_id=snap.global_data.to_play,
            )
        logits, _, _ = self._infer(tensor_dict)
        valid_count = min(len(snap.valid_actions), logits.shape[-1])
        return self._softmax(np.asarray(logits)[0, :valid_count])

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
        if getattr(action, 'decision_value', None) is not None:
            return int(action.decision_value)
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
