# Core 3.4.2 适配测试，验证检查点、特征、效果记忆和宏动作候选

import struct
import tempfile
import unittest
import uuid
from pathlib import Path

import numpy as np
import torch

import feature_encoder
from action_candidates import build_macro_action_pool
from checkpoint_utils import CHECKPOINT_FORMAT_VERSION, load_training_checkpoint
from core.gamestate import DuelState
from data_types import CardEntity, GameAction, GameSnapshot, GlobalFeature
from feature_encoder import GalateaEncoder
from game_constants import LocationInfo, Position, Zone


class EmptySemanticKnowledgeBase:
    # 返回与 3.4.2 编码协议一致的空语义特征
    def get_card_semantics(self, card_id):
        return (
            np.zeros((8, 8), dtype=np.int16),
            np.full((8, 16), -1, dtype=np.int8),
            np.zeros((8, 4), dtype=np.int16),
            np.zeros((8, 4), dtype=np.float16),
            np.zeros((8, 4), dtype=np.int32),
            np.zeros((8, 4), dtype=np.int16),
            np.zeros((8, 4), dtype=np.int16),
            np.zeros((8,), dtype=np.int32),
        )


# 创建测试使用的固定全局状态
def make_global(to_play=0):
    return GlobalFeature(
        turn_count=1,
        phase_id=4,
        to_play=to_play,
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
    )


# 创建测试使用的公开卡片实体
def make_entity(code=12345678):
    return CardEntity(
        code=code,
        owner=0,
        location=Zone.MZONE,
        sequence=0,
        position=Position.FACEUP_ATTACK,
        current_atk=1000,
        current_def=1000,
        type_mask=1,
        race=1,
        attribute=1,
        level=4,
        base_atk=1000,
        base_def=1000,
        is_public=True,
        used_effect_mask=4,
    )


# 创建符合 Core 3.4.2 协议的最小检查点
def make_checkpoint():
    model = torch.nn.Linear(2, 1)
    return {
        "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
        "model_id": str(uuid.uuid4()),
        "model_prefix": "galatea",
        "run_id": "test-run",
        "model_state_dict": model.state_dict(),
        "optimizer_state_dict": {},
        "scaler_state_dict": {},
        "net_config": {
            "d_model": 64,
            "n_heads": 4,
            "n_layers": 1,
            "vocab_size": 20000,
        },
        "iteration": 1,
        "train_step": 1,
        "global_step": 1,
    }


class CoreV342AdapterTests(unittest.TestCase):
    # 验证当前严格检查点协议可以安全加载
    def test_current_checkpoint_protocol_loads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "galatea_iter_1.pth"
            checkpoint = make_checkpoint()
            torch.save(checkpoint, path)

            loaded = load_training_checkpoint(path)

        self.assertEqual(loaded["model_id"], checkpoint["model_id"])

    # 验证旧架构检查点不会被当成新版模型静默加载
    def test_legacy_checkpoint_protocol_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "galatea_iter_1.pth"
            torch.save({"model_state_dict": {"weight": torch.ones(1)}}, path)

            with self.assertRaises(ValueError):
                load_training_checkpoint(path)

    # 验证新版实体特征包含八位效果发动记忆
    def test_encoder_emits_66_card_features(self):
        previous_kb = feature_encoder._GLOBAL_SEM_KB
        feature_encoder._GLOBAL_SEM_KB = EmptySemanticKnowledgeBase()
        try:
            encoder = GalateaEncoder()
            snapshot = GameSnapshot(
                global_data=make_global(),
                entities=[make_entity()],
                valid_actions=[GameAction(action_type=13, index=0)],
            )

            encoded = encoder.encode(snapshot, player_id=0)
        finally:
            feature_encoder._GLOBAL_SEM_KB = previous_kb

        self.assertEqual(tuple(encoded["card_feats"].shape), (1, 120, 66))
        self.assertEqual(encoded["card_feats"][0, 0, 19].item(), 1.0)
        self.assertEqual(tuple(encoded["sem_req"].shape), (1, 120, 8, 16))

    # 验证发动效果会写入槽位掩码并在换回合时清空
    def test_effect_usage_is_tracked_and_reset(self):
        state = DuelState()
        state.field_map[0][Zone.MZONE][0] = {
            "code": 12345678,
            "pos": Position.FACEUP_ATTACK,
            "owner": 0,
            "counters": 0,
            "overlays": [],
            "is_equipped": False,
        }
        chaining = struct.pack(
            "<IIBBBIB",
            12345678,
            LocationInfo.encode(0, Zone.MZONE, 0),
            0,
            Zone.MZONE,
            0,
            5,
            1,
        )

        state.update(70, chaining)
        self.assertEqual(
            state.field_map[0][Zone.MZONE][0]["used_effect_mask"],
            1 << 5,
        )

        state.update(40, b"\x00")
        self.assertEqual(
            state.field_map[0][Zone.MZONE][0]["used_effect_mask"],
            0,
        )

    # 验证多选提示会生成满足最小数量的完整协议响应
    def test_select_card_builds_complete_macro_actions(self):
        state = DuelState()
        entries = []
        for sequence, code in enumerate((11111111, 22222222, 33333333)):
            state.field_map[0][Zone.MZONE][sequence] = {
                "code": code,
                "pos": Position.FACEUP_ATTACK,
                "owner": 0,
                "counters": 0,
                "overlays": [],
                "is_equipped": False,
            }
            entries.append(
                struct.pack("<I", code)
                + bytes([0, Zone.MZONE, sequence, 0])
            )
        payload = bytes([0, 0, 2, 2, 3]) + b"".join(entries)
        state.update(15, payload)
        base_actions = list(state.current_valid_actions)

        macro_actions = build_macro_action_pool(
            15,
            payload,
            state,
            base_actions,
            [1.0, 1.0, 1.0],
        )
        state.current_valid_actions = macro_actions
        snapshot = state.get_snapshot()

        self.assertEqual(len(snapshot.valid_actions), 3)
        self.assertTrue(
            all(action.decision_bytes[0] == 2 for action in snapshot.valid_actions)
        )
        self.assertTrue(
            all(len(action.macro_targets) == 2 for action in snapshot.valid_actions)
        )


if __name__ == "__main__":
    unittest.main()
