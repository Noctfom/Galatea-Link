# 模型协议注册表，负责检查点识别、版本固定和后端构建

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from training_validation import validate_model_prefix


LEGACY_ADAPTER_ID = "core-3.4.2-model-v1"
LEGACY_CHECKPOINT_FORMAT_VERSION = 1
LEGACY_MODEL_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOLS = (LEGACY_MODEL_PROTOCOL_VERSION, 3)


@dataclass(frozen=True)
class ModelBackend:
    """保存已加载网络、编码器和可公开模型身份"""

    net: Any
    encoder: Any
    metadata: dict[str, Any]
    max_actions: int
    adapter_id: str
    inference_runtime: Any = None
    runtime_id: str = "pytorch"


# 安全读取检查点头部并推断旧版缺省协议号
def inspect_model_checkpoint(path: str | Path) -> dict[str, Any]:
    from checkpoint_utils import safe_load_torch_checkpoint

    checkpoint_path = Path(path).resolve()
    checkpoint = safe_load_torch_checkpoint(
        checkpoint_path,
        map_location="cpu",
        materialize_tensors=False,
    )
    if not isinstance(checkpoint, dict):
        raise TypeError("模型检查点必须是字典")

    checkpoint_format = checkpoint.get("checkpoint_format_version")
    explicit_protocol = checkpoint.get("model_protocol_version")
    net_config = checkpoint.get("net_config", {})
    configured_protocol = (
        net_config.get("model_protocol_version")
        if isinstance(net_config, dict)
        else None
    )

    if checkpoint_format == LEGACY_CHECKPOINT_FORMAT_VERSION:
        if explicit_protocol not in (None, LEGACY_MODEL_PROTOCOL_VERSION):
            raise ValueError(
                "旧版检查点声明了冲突的模型协议 "
                f"format={checkpoint_format!r} protocol={explicit_protocol!r}"
            )
        protocol = LEGACY_MODEL_PROTOCOL_VERSION
    elif checkpoint_format == 2 and explicit_protocol == 3:
        if configured_protocol != 3:
            raise ValueError("V3 检查点的网络配置缺少 model_protocol_version=3")
        protocol = 3
    else:
        raise ValueError(
            "不支持的模型检查点协议 "
            f"format={checkpoint_format!r} protocol={explicit_protocol!r} "
            f"supported={SUPPORTED_PROTOCOLS}"
        )

    return {
        "checkpoint_format_version": checkpoint_format,
        "model_protocol_version": protocol,
        "model_id": checkpoint.get("model_id"),
        "model_prefix": checkpoint.get("model_prefix"),
        "iteration": checkpoint.get("iteration"),
        "run_id": checkpoint.get("run_id"),
        "net_config": dict(net_config) if isinstance(net_config, dict) else {},
    }


# 将外部协议固定值转换为内部数字并保留自动识别
def _normalize_protocol_pin(protocol: str | int | None) -> int | None:
    if protocol is None:
        return None
    normalized = str(protocol).strip().casefold()
    if normalized in {"", "auto"}:
        return None
    aliases = {
        "1": 1,
        "v1": 1,
        "legacy": 1,
        "core-3.4.2": 1,
        "3": 3,
        "v3": 3,
        "core-3.6.3": 3,
        "core-3.6.5": 3,
    }
    if normalized not in aliases:
        raise ValueError(f"未知模型协议固定值: {protocol!r}")
    return aliases[normalized]


# 校验用户固定的协议必须与检查点真实协议一致
def _validate_protocol_pin(protocol: str | int | None, actual: int) -> None:
    pinned = _normalize_protocol_pin(protocol)
    if pinned is not None and pinned != actual:
        raise ValueError(
            f"模型协议固定值与检查点不一致 pinned={pinned} actual={actual}"
        )


# 从显式配置或检查点相邻目录定位 V3 语义资产
def _resolve_v3_asset_dir(
    checkpoint_path: Path,
    configured_path: str | Path | None,
) -> Path:
    from .v3.constants import REQUIRED_ASSET_FILENAMES

    if configured_path:
        candidates = [Path(configured_path).expanduser().resolve()]
    else:
        candidates = [
            checkpoint_path.parent,
            checkpoint_path.parent.parent,
            Path(__file__).resolve().parents[1] / "model_assets" / "v3",
        ]

    for candidate in candidates:
        if all((candidate / name).is_file() for name in REQUIRED_ASSET_FILENAMES):
            return candidate

    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        "找不到模型协议 V3 语义资产 "
        f"required={REQUIRED_ASSET_FILENAMES} searched={searched}"
    )


# 校验模型协议 V3 语义资产内部映射完整一致
def _validate_v3_asset_bundle(asset_dir: Path) -> None:
    from .v3.semantic_assets import validate_semantic_bundle

    validate_semantic_bundle(asset_dir)


# 校验 V3 检查点身份和网络结构必需字段
def _validate_v3_checkpoint(checkpoint: Any) -> dict[str, Any]:
    from checkpoint_utils import validate_model_id

    required = {
        "checkpoint_format_version",
        "model_protocol_version",
        "model_id",
        "model_prefix",
        "run_id",
        "model_state_dict",
        "net_config",
        "iteration",
    }
    if not isinstance(checkpoint, dict):
        raise TypeError("V3 模型检查点必须是字典")
    missing = sorted(required.difference(checkpoint))
    if missing:
        raise KeyError(f"V3 模型检查点缺少字段: {missing}")
    if checkpoint["checkpoint_format_version"] != 2:
        raise ValueError("V3 模型检查点格式必须为 2")
    if checkpoint["model_protocol_version"] != 3:
        raise ValueError("V3 模型协议必须为 3")
    validate_model_id(checkpoint["model_id"])
    validate_model_prefix(checkpoint["model_prefix"])
    net_config = checkpoint["net_config"]
    if not isinstance(net_config, dict) or net_config.get("model_protocol_version") != 3:
        raise ValueError("V3 网络配置必须声明 model_protocol_version=3")
    state_dict = checkpoint["model_state_dict"]
    if not isinstance(state_dict, dict) or not state_dict:
        raise ValueError("V3 模型权重不能为空")
    return checkpoint


# 加载旧版 Core 3.4.2 模型后端
def _load_legacy_backend(path: Path, device: str) -> ModelBackend:
    from checkpoint_utils import load_training_checkpoint as load_legacy_checkpoint
    from feature_encoder import GalateaEncoder
    from galatea_net import GalateaNet
    from .inference_runtime import PyTorchInferenceRuntime

    checkpoint = load_legacy_checkpoint(path, map_location=device)
    net = GalateaNet(checkpoint["net_config"]).to(device)
    net.load_state_dict(checkpoint["model_state_dict"], strict=True)
    net.eval()
    encoder = GalateaEncoder()
    metadata = _build_public_metadata(
        checkpoint,
        adapter_id=LEGACY_ADAPTER_ID,
        model_protocol_version=LEGACY_MODEL_PROTOCOL_VERSION,
        assets_verified=False,
    )
    runtime = PyTorchInferenceRuntime(net, device)
    metadata["inference_backend"] = runtime.runtime_id
    return ModelBackend(
        net,
        encoder,
        metadata,
        80,
        LEGACY_ADAPTER_ID,
        runtime,
        runtime.runtime_id,
    )


# 加载隔离的模型协议 V3 后端
def _load_v3_backend(
    path: Path,
    device: str,
    asset_path: str | Path | None,
    strict_asset_hashes: bool,
) -> ModelBackend:
    from checkpoint_utils import safe_load_torch_checkpoint
    from .v3.constants import ADAPTER_ID, MAX_ACTIONS
    from .v3.feature_encoder import GalateaEncoder
    from .v3.galatea_net import GalateaNet
    from .inference_runtime import PyTorchInferenceRuntime

    asset_dir = _resolve_v3_asset_dir(path, asset_path)
    if strict_asset_hashes:
        _validate_v3_asset_bundle(asset_dir)
    checkpoint = _validate_v3_checkpoint(
        safe_load_torch_checkpoint(path, map_location=device)
    )
    net = GalateaNet(checkpoint["net_config"], asset_dir=asset_dir).to(device)
    net.load_state_dict(checkpoint["model_state_dict"], strict=True)
    net.eval()
    encoder = GalateaEncoder(asset_dir=asset_dir)
    metadata = _build_public_metadata(
        checkpoint,
        adapter_id=ADAPTER_ID,
        model_protocol_version=3,
        assets_verified=bool(strict_asset_hashes),
    )
    runtime = PyTorchInferenceRuntime(net, device)
    metadata["inference_backend"] = runtime.runtime_id
    return ModelBackend(
        net,
        encoder,
        metadata,
        MAX_ACTIONS,
        ADAPTER_ID,
        runtime,
        runtime.runtime_id,
    )


# 读取并严格校验模型协议 V3 ONNX 轮次清单
def inspect_onnx_artifact(path: str | Path) -> dict[str, Any]:
    graph_path = Path(path).expanduser().resolve()
    if graph_path.suffix.casefold() != ".onnx":
        raise ValueError("ONNX 模型主图必须使用 .onnx 扩展名")
    if not graph_path.is_file():
        raise FileNotFoundError(f"ONNX 模型主图不存在: {graph_path}")
    manifest_path = graph_path.with_suffix(".artifacts.json")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"ONNX 轮次清单不存在: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    if not isinstance(manifest, dict):
        raise TypeError("ONNX 轮次清单必须是 JSON 对象")
    onnx_record = manifest.get("onnx")
    if not isinstance(onnx_record, dict):
        raise ValueError("ONNX 轮次清单缺少 onnx 记录")
    if manifest.get("artifact_manifest_version") != 2:
        raise ValueError("模型协议 V3 仅支持制品清单版本 2")
    if manifest.get("model_protocol_version") != 3:
        raise ValueError("当前 ONNX 适配器仅支持模型协议 V3")
    if onnx_record.get("model_protocol_version") != 3:
        raise ValueError("ONNX 记录未声明模型协议 V3")
    if onnx_record.get("status") != "complete":
        raise ValueError("ONNX 制品尚未完整导出")
    if onnx_record.get("primary") != graph_path.name:
        raise ValueError("ONNX 主图名称与轮次清单不一致")
    files = onnx_record.get("files")
    if not isinstance(files, list) or graph_path.name not in files:
        raise ValueError("ONNX 轮次清单文件列表不完整")
    for filename in files:
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("ONNX 轮次清单包含不安全文件名")
        if not (graph_path.parent / filename).is_file():
            raise FileNotFoundError(f"ONNX 制品文件不存在: {filename}")
    model_id = manifest.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("ONNX 轮次清单缺少模型 UUID")
    try:
        uuid.UUID(model_id)
    except (ValueError, TypeError) as error:
        raise ValueError("ONNX 轮次清单模型 UUID 无效") from error
    model_prefix = validate_model_prefix(manifest.get("model_prefix"))
    if onnx_record.get("model_id") != model_id:
        raise ValueError("ONNX 模型 UUID 与轮次清单不一致")
    if onnx_record.get("model_prefix") != model_prefix:
        raise ValueError("ONNX 模型前缀与轮次清单不一致")
    return {
        "checkpoint_format_version": manifest.get("checkpoint_format_version"),
        "model_protocol_version": 3,
        "model_id": model_id,
        "model_prefix": model_prefix,
        "iteration": manifest.get("iteration"),
        "run_id": manifest.get("run_id"),
        "net_config": {},
        "manifest_version": manifest.get("artifact_manifest_version"),
    }


# 加载隔离的模型协议 V3 ONNX Runtime 后端
def _load_v3_onnx_backend(
    path: Path,
    inspected: dict[str, Any],
    asset_path: str | Path | None,
    strict_asset_hashes: bool,
    providers: tuple[str, ...],
    intra_op_threads: int,
) -> ModelBackend:
    from .inference_runtime import OnnxInferenceRuntime
    from .v3.constants import ADAPTER_ID, MAX_ACTIONS
    from .v3.feature_encoder import GalateaEncoder

    asset_dir = _resolve_v3_asset_dir(path, asset_path)
    if strict_asset_hashes:
        _validate_v3_asset_bundle(asset_dir)
    runtime = OnnxInferenceRuntime(
        path,
        providers=providers,
        intra_op_threads=intra_op_threads,
    )
    graph_metadata = runtime.metadata
    expected_graph_metadata = {
        "galatea.model_id": str(inspected["model_id"]),
        "galatea.model_prefix": str(inspected["model_prefix"]),
        "galatea.iteration": str(inspected["iteration"]),
        "galatea.model_protocol_version": "3",
    }
    mismatches = {
        key: {"expected": value, "actual": graph_metadata.get(key)}
        for key, value in expected_graph_metadata.items()
        if graph_metadata.get(key) != value
    }
    if mismatches:
        raise ValueError(f"ONNX 图身份元数据不一致: {mismatches}")
    metadata = dict(inspected)
    metadata.update(
        {
            "adapter_id": ADAPTER_ID,
            "assets_verified": bool(strict_asset_hashes),
            "inference_backend": runtime.runtime_id,
            "execution_providers": list(runtime.session.get_providers()),
        }
    )
    return ModelBackend(
        None,
        GalateaEncoder(asset_dir=asset_dir),
        metadata,
        MAX_ACTIONS,
        ADAPTER_ID,
        runtime,
        runtime.runtime_id,
    )


# 构建不会泄露本地资源路径的模型公开身份
def _build_public_metadata(
    checkpoint: dict[str, Any],
    *,
    adapter_id: str,
    model_protocol_version: int,
    assets_verified: bool,
) -> dict[str, Any]:
    return {
        "adapter_id": adapter_id,
        "checkpoint_format_version": checkpoint["checkpoint_format_version"],
        "model_protocol_version": model_protocol_version,
        "model_id": checkpoint["model_id"],
        "model_prefix": checkpoint["model_prefix"],
        "iteration": checkpoint["iteration"],
        "run_id": checkpoint["run_id"],
        "net_config": dict(checkpoint["net_config"]),
        "assets_verified": assets_verified,
    }


# 自动选择协议适配器并严格加载对应模型
def load_model_backend(
    path: str | Path,
    *,
    device: str = "cpu",
    expected_model_id: str | None = None,
    protocol: str | int | None = "auto",
    asset_path: str | Path | None = None,
    strict_asset_hashes: bool = True,
    inference_backend: str = "auto",
    onnx_providers: tuple[str, ...] = ("CPUExecutionProvider",),
    onnx_intra_op_threads: int = 0,
) -> ModelBackend:
    checkpoint_path = Path(path).resolve()
    requested_backend = str(inference_backend).strip().casefold()
    if requested_backend not in {"auto", "pytorch", "onnxruntime"}:
        raise ValueError("inference_backend 必须是 auto、pytorch 或 onnxruntime")
    artifact_backend = (
        "onnxruntime"
        if checkpoint_path.suffix.casefold() == ".onnx"
        else "pytorch"
    )
    if requested_backend != "auto" and requested_backend != artifact_backend:
        raise ValueError(
            "推理后端与模型文件类型不一致 "
            f"backend={requested_backend} path={checkpoint_path.name}"
        )
    if artifact_backend == "onnxruntime":
        inspected = inspect_onnx_artifact(checkpoint_path)
        _validate_protocol_pin(protocol, inspected["model_protocol_version"])
        if expected_model_id and inspected["model_id"] != expected_model_id:
            raise PermissionError(
                "模型 UUID 校验失败 "
                f"期望 {expected_model_id} 实际 {inspected['model_id']}"
            )
        return _load_v3_onnx_backend(
            checkpoint_path,
            inspected,
            asset_path,
            strict_asset_hashes,
            tuple(onnx_providers),
            int(onnx_intra_op_threads),
        )

    inspected = inspect_model_checkpoint(checkpoint_path)
    actual_protocol = inspected["model_protocol_version"]
    _validate_protocol_pin(protocol, actual_protocol)
    if expected_model_id and inspected["model_id"] != expected_model_id:
        raise PermissionError(
            "模型 UUID 校验失败 "
            f"期望 {expected_model_id} 实际 {inspected['model_id']}"
        )

    if actual_protocol == LEGACY_MODEL_PROTOCOL_VERSION:
        return _load_legacy_backend(checkpoint_path, device)
    if actual_protocol == 3:
        return _load_v3_backend(
            checkpoint_path,
            device,
            asset_path,
            strict_asset_hashes,
        )
    raise AssertionError(f"已识别但未注册的模型协议: {actual_protocol}")
