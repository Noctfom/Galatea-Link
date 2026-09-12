# Core 3.4.2 模型冒烟脚本，验证严格加载、特征编码和单次本地推理

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.ai_bot import AiBot, _console_print
from data_types import CardEntity, GameAction, GameSnapshot, GlobalFeature
from game_constants import Position, Zone


# 构建不连接游戏服务器的最小合法决策快照
def build_snapshot():
    global_data = GlobalFeature(
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
    )
    entity = CardEntity(
        code=89631139,
        owner=0,
        location=Zone.MZONE,
        sequence=0,
        position=Position.FACEUP_ATTACK,
        current_atk=3000,
        current_def=2500,
        type_mask=1,
        race=0x2000,
        attribute=0x10,
        level=8,
        base_atk=3000,
        base_def=2500,
        is_public=True,
    )
    return GameSnapshot(
        global_data=global_data,
        entities=[entity],
        valid_actions=[
            GameAction(action_type=13, index=0, desc_str="No"),
            GameAction(action_type=13, index=1, desc_str="Yes"),
        ],
    )


# 解析参数并执行一次完整本地模型推理
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--weights",
        default=str(PROJECT_ROOT / "models" / "galatea_iter_30.pth"),
    )
    parser.add_argument(
        "--expected-model-id",
        default="a204dd97-0f6f-46dc-9fb2-aba65df10e0c",
    )
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()

    bot = AiBot(device=args.device, initialize_network=False)
    if not bot.load_model(
        args.weights,
        expected_model_id=args.expected_model_id or None,
    ):
        raise SystemExit(1)

    decision = bot.get_scored_decision_from_snapshot(build_snapshot(), 13)
    if decision is None:
        raise RuntimeError("模型没有返回冒烟决策")
    _console_print(
        "✅ Core 3.4.2 模型冒烟成功 "
        f"choice_id={decision.choice_id} "
        f"confidence={decision.confidence:.4f} "
        f"model_id={bot.model_metadata['model_id']}"
    )


if __name__ == "__main__":
    main()
