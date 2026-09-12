# 模型协议适配入口，负责按检查点元数据选择隔离实现

from .registry import (
    ModelBackend,
    inspect_model_checkpoint,
    inspect_onnx_artifact,
    load_model_backend,
)


__all__ = [
    "ModelBackend",
    "inspect_model_checkpoint",
    "inspect_onnx_artifact",
    "load_model_backend",
]
