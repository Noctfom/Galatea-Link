# 模型协议兼容测试脚本，检查元数据并可执行一次完整 V3 推理

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_types import CardEntity, GameAction, GameSnapshot, GlobalFeature
from game_constants import LocationInfo, Zone
from model_protocols import (
    inspect_model_checkpoint,
    inspect_onnx_artifact,
    load_model_backend,
)


# 构建不会依赖在线房间的最小公开测试快照
def build_smoke_snapshot():
    location = LocationInfo.encode(0, Zone.MZONE, 0, 1)
    entity = CardEntity(
        code=89631139,
        owner=0,
        location=Zone.MZONE,
        sequence=0,
        position=1,
        current_atk=3000,
        current_def=2500,
        type_mask=1,
        race=8192,
        attribute=16,
        level=8,
        base_atk=3000,
        base_def=2500,
        is_public=True,
    )
    action = GameAction(
        action_type=13,
        index=0,
        desc_str="No",
        operation_id=2,
        response_value=0,
        target_entity_idx=0,
        target_location_raw=location,
    )
    return GameSnapshot(
        global_data=GlobalFeature(
            turn_count=1,
            phase_id=1,
            to_play=0,
            my_lp=8000,
            op_lp=8000,
            my_hand_len=5,
            op_hand_len=5,
            my_deck_len=35,
            op_deck_len=35,
            my_grave_len=0,
            op_grave_len=0,
            my_removed_len=0,
            op_removed_len=0,
            my_extra_len=15,
            op_extra_len=15,
        ),
        entities=[entity],
        valid_actions=[action],
    )


# 解析命令行并执行协议探测或完整推理
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint")
    parser.add_argument("--assets", default="")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    checkpoint_path = Path(args.checkpoint)
    metadata = (
        inspect_onnx_artifact(checkpoint_path)
        if checkpoint_path.suffix.casefold() == ".onnx"
        else inspect_model_checkpoint(checkpoint_path)
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    if not args.full:
        return

    backend = load_model_backend(
        args.checkpoint,
        device=args.device,
        asset_path=args.assets or None,
    )
    output_format = "numpy" if backend.runtime_id == "onnxruntime" else "torch"
    tensors = backend.encoder.encode(
        build_smoke_snapshot(),
        player_id=0,
        output_format=output_format,
    )
    logits, value, _ = backend.inference_runtime.infer(tensors)
    print(
        "完整推理成功 "
        f"adapter={backend.adapter_id} "
        f"logits_shape={tuple(logits.shape)} "
        f"value_shape={tuple(value.shape)}"
    )


if __name__ == "__main__":
    main()
