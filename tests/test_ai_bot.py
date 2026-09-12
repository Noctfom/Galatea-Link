# Galatea 策略测试，验证异常空快照会交给规则兜底

import unittest

import numpy as np
import torch

from agents.ai_bot import AiBot
from data_types import GameAction, GameSnapshot, GlobalFeature


class EvalOnlyNet:
    # 记录策略是否进入网络前向计算
    def __init__(self):
        self.eval_called = False

    # 模拟模型切换到推理模式
    def eval(self):
        self.eval_called = True


class FixedLogitNet:
    # 初始化固定合法动作分数
    def __init__(self):
        self.eval_called = False

    # 模拟模型切换到推理模式
    def eval(self):
        self.eval_called = True

    # 返回两个动作的固定分数
    def __call__(self, observation):
        return torch.tensor([[2.0, 1.0]]), torch.tensor([[0.0]]), None


class EmptyEncoder:
    # 返回无需特征的测试张量映射
    def encode(self, snapshot, player_id):
        return {}


class FixedChoiceRng:
    # 初始化固定采样结果并保存收到的概率
    def __init__(self, selected_index):
        self.selected_index = selected_index
        self.probabilities = None

    # 返回固定动作编号以验证部署温度分布
    def choice(self, size, *, p):
        self.probabilities = np.asarray(p)
        return self.selected_index


class AiBotTests(unittest.TestCase):
    # 验证没有任何实体的快照不会触发 Transformer 前向计算
    def test_empty_snapshot_returns_rule_fallback(self):
        bot = AiBot.__new__(AiBot)
        bot.net = EvalOnlyNet()
        snapshot = GameSnapshot(
            global_data=GlobalFeature(
                turn_count=0,
                phase_id=0,
                to_play=0,
                my_lp=8000,
                op_lp=8000,
                my_hand_len=0,
                op_hand_len=0,
                my_deck_len=0,
                op_deck_len=0,
                my_grave_len=0,
                op_grave_len=0,
                my_removed_len=0,
                op_removed_len=0,
                my_extra_len=0,
                op_extra_len=0,
            ),
            entities=[],
            valid_actions=[GameAction(action_type=13, index=0)],
        )

        result = bot.get_decision_from_snapshot(snapshot, 13)

        self.assertIsNone(result)
        self.assertTrue(bot.net.eval_called)

    # 验证 Core 返回动作编号、协议响应和 softmax 置信度
    def test_scored_decision_exposes_confidence(self):
        bot = AiBot.__new__(AiBot)
        bot.net = FixedLogitNet()
        bot.encoder = EmptyEncoder()
        bot.device = "cpu"
        snapshot = GameSnapshot(
            global_data=GlobalFeature(
                turn_count=1,
                phase_id=4,
                to_play=0,
                my_lp=8000,
                op_lp=8000,
                my_hand_len=1,
                op_hand_len=1,
                my_deck_len=39,
                op_deck_len=39,
                my_grave_len=0,
                op_grave_len=0,
                my_removed_len=0,
                op_removed_len=0,
                my_extra_len=15,
                op_extra_len=15,
            ),
            entities=[object()],
            valid_actions=[
                GameAction(action_type=13, index=1),
                GameAction(action_type=13, index=0),
            ],
        )

        result = bot.get_scored_decision_from_snapshot(snapshot, 13)

        self.assertEqual(result.choice_id, 0)
        self.assertEqual(result.response, 1)
        self.assertAlmostEqual(result.confidence, 0.7310586, places=5)
        self.assertAlmostEqual(result.probability_margin, 0.4621172, places=5)

    # 验证部署策略按温度概率采样且返回真实采样置信度
    def test_deployment_policy_samples_temperature_distribution(self):
        bot = AiBot.__new__(AiBot)
        bot.net = FixedLogitNet()
        bot.encoder = EmptyEncoder()
        bot.device = "cpu"
        bot._rng = FixedChoiceRng(1)
        snapshot = GameSnapshot(
            global_data=GlobalFeature(
                turn_count=1,
                phase_id=4,
                to_play=0,
                my_lp=8000,
                op_lp=8000,
                my_hand_len=1,
                op_hand_len=1,
                my_deck_len=39,
                op_deck_len=39,
                my_grave_len=0,
                op_grave_len=0,
                my_removed_len=0,
                op_removed_len=0,
                my_extra_len=15,
                op_extra_len=15,
            ),
            entities=[object()],
            valid_actions=[
                GameAction(action_type=13, index=1),
                GameAction(action_type=13, index=0),
            ],
        )

        result = bot.get_scored_decision_from_snapshot(
            snapshot,
            13,
            policy_mode="deployment",
            temperature=2.0,
        )

        self.assertEqual(result.choice_id, 1)
        self.assertEqual(result.response, 0)
        self.assertEqual(result.policy_mode, "deployment")
        self.assertEqual(result.temperature, 2.0)
        self.assertAlmostEqual(result.confidence, 0.3775407, places=5)
        self.assertAlmostEqual(bot._rng.probabilities[0], 0.6224593, places=5)

    # 验证非法部署温度在模型采样前直接失败
    def test_rejects_invalid_core_temperature(self):
        bot = AiBot.__new__(AiBot)
        bot.net = FixedLogitNet()
        bot.encoder = EmptyEncoder()
        bot.device = "cpu"
        snapshot = GameSnapshot(
            global_data=GlobalFeature(
                turn_count=1,
                phase_id=4,
                to_play=0,
                my_lp=8000,
                op_lp=8000,
                my_hand_len=1,
                op_hand_len=1,
                my_deck_len=39,
                op_deck_len=39,
                my_grave_len=0,
                op_grave_len=0,
                my_removed_len=0,
                op_removed_len=0,
                my_extra_len=15,
                op_extra_len=15,
            ),
            entities=[object()],
            valid_actions=[GameAction(action_type=13, index=0)],
        )

        with self.assertRaisesRegex(ValueError, "温度"):
            bot.get_scored_decision_from_snapshot(
                snapshot,
                13,
                policy_mode="deployment",
                temperature=0.0,
            )


if __name__ == "__main__":
    unittest.main()
