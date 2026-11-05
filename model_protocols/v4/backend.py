# V4 检查点和 ONNX 运行时接入，严格验证身份并支持玩家观测兼容推理

from pathlib import Path
import uuid

from . import ADAPTER_ID, MAX_ACTIONS, REQUIRED_ASSET_FILENAMES
from .assets import PROTOCOL_IDENTITY_KEYS, inspect_protocol_identity, load_v4_assets
from .encoder import GalateaEncoder
from .runtime_context import use_assets


# 在分配模型前验证通用身份与 V4 固定结构
def inspect_v4_metadata(metadata):
    identity = inspect_protocol_identity(metadata)
    if metadata.get("checkpoint_format_version") != 3:
        raise ValueError("V4 检查点格式必须为 3")
    model_id = metadata.get("model_id")
    if not isinstance(model_id, str) or str(uuid.UUID(model_id)) != model_id:
        raise ValueError("V4 模型 UUID 无效")
    from training_validation import validate_model_prefix
    validate_model_prefix(metadata.get("model_prefix"))
    if type(metadata.get("iteration")) is not int or metadata["iteration"] < 0:
        raise ValueError("V4 模型轮次无效")
    config = metadata.get("net_config")
    if config is not None:
        if not isinstance(config, dict):
            raise ValueError("V4 网络配置必须是对象")
        for key, expected in identity.items():
            if config.get(key) != expected:
                raise ValueError(f"V4 网络配置与检查点身份不一致: {key}")
    return identity


# 核对 ONNX 制品清单及当前轮次的完整文件集合
def inspect_v4_onnx(path, manifest):
    inspect_v4_metadata(manifest)
    if manifest.get("artifact_manifest_version") != 3:
        raise ValueError("V4 制品清单格式必须为 3")
    record = manifest.get("onnx")
    if not isinstance(record, dict) or record.get("status") != "complete" or record.get("primary") != path.name:
        raise ValueError("V4 ONNX 制品尚未完整导出或主图名称不一致")
    for key in (*PROTOCOL_IDENTITY_KEYS, "model_id", "model_prefix", "iteration"):
        if record.get(key) != manifest.get(key):
            raise ValueError(f"V4 ONNX 记录与轮次清单不一致: {key}")
    files = record.get("files")
    if not isinstance(files, list) or path.name not in files or any(not isinstance(name, str) for name in files) or len(files) != len(set(files)):
        raise ValueError("V4 ONNX 制品文件列表无效")
    for name in files:
        if name != Path(name).name or "/" in name or "\\" in name or ".." in name or not name.endswith((".onnx", ".onnx.data")):
            raise ValueError("V4 ONNX 制品包含不安全文件名")
        member = path.parent / name
        if member.is_symlink() or not member.is_file():
            raise FileNotFoundError(f"V4 ONNX 制品文件不存在: {name}")
    validate_onnx_external_data(path, record)
    return {**{key: manifest.get(key) for key in (*PROTOCOL_IDENTITY_KEYS, "checkpoint_format_version", "model_id", "model_prefix", "iteration", "run_id", "training_stage", "training_lineage")}, "net_config": {}, "manifest_version": 3}


# 在创建 ORT 会话前检查图中全部外置张量引用且不读取外部权重
def validate_onnx_external_data(path, record):
    try:
        import onnx
    except ImportError as error:
        raise RuntimeError("V4 ONNX 安全检查需要 onnx，请安装 requirements-onnx.txt") from error
    if path.stat().st_size > 2 * 1024 * 1024 * 1024:
        raise ValueError("ONNX 主图超过 2 GiB 安全限制")
    model = onnx.load(str(path), load_external_data=False)
    pending, external, visited = [model], set(), 0
    while pending:
        item = pending.pop()
        visited += 1
        if visited > 1_000_000:
            raise ValueError("ONNX 图结构超过安全限制")
        if isinstance(item, onnx.TensorProto) and item.data_location == onnx.TensorProto.EXTERNAL:
            entries = {entry.key: entry.value for entry in item.external_data}
            if len(entries) != len(item.external_data) or set(entries) - {"location", "offset", "length", "checksum"}:
                raise ValueError("ONNX 外置张量参数无效")
            location = entries.get("location")
            if location not in record["files"] or location == path.name:
                raise ValueError("ONNX 外置张量引用未声明或越界文件")
            try:
                offset = int(entries.get("offset", "0"))
                size = (path.parent / location).stat().st_size
                length = int(entries.get("length", str(size - offset)))
            except (ValueError, OSError) as error:
                raise ValueError("ONNX 外置张量范围无效") from error
            if offset < 0 or length < 0 or offset + length > size:
                raise ValueError("ONNX 外置张量范围越界")
            external.add(location)
        for field, value in item.ListFields():
            if field.type == field.TYPE_MESSAGE:
                pending.extend(value if field.is_repeated else [value])
    declared = record.get("external_data")
    if not isinstance(declared, list) or any(not isinstance(name, str) for name in declared) or len(declared) != len(set(declared)) or set(declared) != external or set(record["files"]) != {path.name, *external}:
        raise ValueError("ONNX 外置权重清单与主图引用不一致")


# 从显式目录或模型专属目录寻找 V4 编译资产且不回落到 V3
def resolve_asset_directory(path, asset_path, model_id=None):
    candidates = [Path(asset_path).expanduser().resolve()] if asset_path else [path.parent, path.parent.parent]
    if not asset_path:
        asset_root = path.parent.parent / "model_assets" / "v4"
        candidates.extend([asset_root / model_id, asset_root] if model_id else [asset_root])
    for candidate in candidates:
        if all((candidate / name).is_file() for name in REQUIRED_ASSET_FILENAMES):
            return candidate
    raise FileNotFoundError("找不到 V4 精确词表及编译语义资产，请导入 GKG4 或指定 assets_path")


# 公布兼容在线能力与历史输入策略，完整原生观测资格单独保留
def public_metadata(inspected, assets, runtime):
    metadata = {**inspected, "adapter_id": ADAPTER_ID, "assets_verified": True,
            "inference_backend": runtime.runtime_id, "offline_inference_ready": True,
            "online_inference_ready": True,
            "online_inference_mode": "player_view_compatible_v1",
            "online_history_policy": "masked_unavailable",
            "native_online_observation_complete": False,
            "spectator_evaluation_supported": False,
            "runtime_asset_identity": dict(assets.identity),
            "online_block_reason": None,
            "online_warning": "V4 使用玩家可见状态兼容推理，无法对齐的转移历史已屏蔽，策略质量待真实对局验证"}
    if runtime.runtime_id == "onnxruntime":
        metadata["asset_compatibility"] = assets.onnx_compatibility(inspected)
    elif inspected["semantic_lookup_hash"] != assets.lookup.prefix_hash(inspected["semantic_lookup_card_count"]):
        metadata["semantic_update"] = {"status": "compatible_content_update",
            "source_semantic_lookup_hash": inspected["semantic_lookup_hash"],
            "runtime_semantic_lookup_hash": assets.identity["semantic_lookup_hash"],
            "message": "已校验语义编号和向量坐标系，使用新语义内容，模型预测可能变化"}
    return metadata


# 加载独立 V4 模型及运行时且不导入外部 Core 模块
def load_v4_backend(path, inspected, *, device="cpu", asset_path=None, providers=("CPUExecutionProvider",), intra_op_threads=0):
    from model_protocols.registry import ModelBackend
    from model_protocols.inference_runtime import OnnxInferenceRuntime, PyTorchInferenceRuntime
    assets = load_v4_assets(resolve_asset_directory(path, asset_path, inspected["model_id"]))
    frozen_onnx = path.suffix.casefold() == ".onnx"
    assets.validate(inspected, allow_semantic_updates=not frozen_onnx, frozen_onnx=frozen_onnx)
    net = None
    if path.suffix.casefold() == ".onnx":
        runtime = OnnxInferenceRuntime(path, providers=providers, intra_op_threads=intra_op_threads)
        for key in (*PROTOCOL_IDENTITY_KEYS, "model_id", "model_prefix", "iteration"):
            if runtime.metadata.get(f"galatea.{key}") != str(inspected[key]):
                raise ValueError(f"V4 ONNX 图身份不一致: {key}")
    else:
        from checkpoint_utils import safe_load_torch_checkpoint
        from .galatea_net import GalateaNet
        checkpoint = safe_load_torch_checkpoint(path, map_location=device)
        inspect_v4_metadata(checkpoint)
        assets.validate(checkpoint, allow_semantic_updates=True)
        for key in (*PROTOCOL_IDENTITY_KEYS, "model_id", "model_prefix", "iteration"):
            if checkpoint.get(key) != inspected.get(key):
                raise ValueError(f"V4 检查点在探测后发生变化: {key}")
        if not isinstance(checkpoint.get("model_state_dict"), dict) or not checkpoint["model_state_dict"]:
            raise ValueError("V4 模型权重不能为空")
        with use_assets(assets):
            net = GalateaNet({**checkpoint["net_config"], **assets.identity}).to(device)
        net.load_state_dict(checkpoint["model_state_dict"], strict=True)
        net.eval()
        runtime = PyTorchInferenceRuntime(net, device)
    encoder = GalateaEncoder(assets, frozen_card_count=inspected["card_vocab_card_count"] if frozen_onnx else None)
    return ModelBackend(net, encoder, public_metadata(inspected, assets, runtime), MAX_ACTIONS, ADAPTER_ID, runtime, runtime.runtime_id)
