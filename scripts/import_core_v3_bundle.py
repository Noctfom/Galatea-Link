# 模型协议 V3 目录导入脚本，只读 Core 并复制模型与一致的语义资产

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_protocols import inspect_model_checkpoint, inspect_onnx_artifact
from model_protocols.v3.constants import REQUIRED_ASSET_FILENAMES
from model_protocols.v3.semantic_assets import validate_semantic_bundle
from service.model_repository import validate_safe_filename


OPTIONAL_ASSET_FILENAMES = (
    "hash_mapping_report.json",
    "meta_staples.json",
    "cards.cdb",
)


# 通过同目录临时文件完成可恢复的单文件替换
def copy_atomically(source, destination, overwrite=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"目标文件已存在: {destination}")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".import.tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
        shutil.copy2(source, temporary_path)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


# 从 ONNX 制品清单收集主图、外置权重和清单文件
def collect_onnx_artifacts(graph_path):
    manifest_path = graph_path.with_suffix(".artifacts.json")
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    record = manifest.get("onnx") or {}
    filenames = record.get("files")
    if not isinstance(filenames, list) or graph_path.name not in filenames:
        raise ValueError("ONNX 制品清单文件列表无效")
    artifacts = []
    for filename in [*filenames, manifest_path.name]:
        safe_name = validate_safe_filename(
            filename,
            allowed_suffixes=(".onnx", ".onnx.data", ".artifacts.json"),
        )
        source = graph_path.parent / safe_name
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"缺少 ONNX 制品文件: {safe_name}")
        if source not in artifacts:
            artifacts.append(source)
    return artifacts


# 校验源模型均属于模型协议 V3 且共享模型身份
def validate_source(core_root, iteration, artifact_format):
    model_dir = core_root / "models"
    stem = f"galatea_iter_{iteration}"
    artifacts = []
    metadata_records = []
    if artifact_format in {"pytorch", "both"}:
        checkpoint = model_dir / f"{stem}.pth"
        metadata = inspect_model_checkpoint(checkpoint)
        if metadata["model_protocol_version"] != 3:
            raise ValueError("源检查点不是模型协议 V3")
        artifacts.append(checkpoint)
        marker = checkpoint.with_suffix(".artifacts.json")
        if marker.is_file():
            artifacts.append(marker)
        metadata_records.append(metadata)
    if artifact_format in {"onnxruntime", "both"}:
        graph = model_dir / f"{stem}.onnx"
        metadata = inspect_onnx_artifact(graph)
        artifacts.extend(collect_onnx_artifacts(graph))
        metadata_records.append(metadata)

    identities = {
        (
            item.get("model_id"),
            item.get("model_prefix"),
            item.get("iteration"),
            item.get("model_protocol_version"),
        )
        for item in metadata_records
    }
    if len(identities) != 1:
        raise ValueError("所选模型制品的 UUID、前缀、轮次或协议不一致")
    validate_semantic_bundle(core_root)
    return list(dict.fromkeys(artifacts)), metadata_records[0]


# 解析参数并整理为 Link 可直接选择的模型协议 V3 目录布局
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--core-root",
        default=str(PROJECT_ROOT.parent / "Galatea_Core"),
    )
    parser.add_argument("--iteration", type=int, default=100)
    parser.add_argument(
        "--format",
        choices=("onnxruntime", "pytorch", "both"),
        default="onnxruntime",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    core_root = Path(args.core_root).expanduser().resolve()
    artifacts, metadata = validate_source(
        core_root,
        args.iteration,
        args.format,
    )
    model_destination = PROJECT_ROOT / "models"
    asset_destination = (
        PROJECT_ROOT
        / "model_assets"
        / "v3"
        / str(metadata["model_id"])
    )

    for artifact in artifacts:
        copy_atomically(
            artifact,
            model_destination / artifact.name,
            args.overwrite,
        )
    for filename in (*REQUIRED_ASSET_FILENAMES, *OPTIONAL_ASSET_FILENAMES):
        source = core_root / filename
        if source.is_file():
            copy_atomically(
                source,
                asset_destination / filename,
                args.overwrite,
            )
    validate_semantic_bundle(asset_destination)

    release_path = core_root / "version.txt"
    release = (
        release_path.read_text(encoding="utf-8-sig").strip()
        if release_path.is_file()
        else "unknown"
    )
    print(
        "模型协议 V3 导入成功\n"
        f"source_core_release: {release}\n"
        f"format: {args.format}\n"
        f"weights_path: ./models/{artifacts[0].name}\n"
        f"assets_path: ./{asset_destination.relative_to(PROJECT_ROOT).as_posix()}\n"
        f"expected_model_id: {metadata['model_id']}"
    )


if __name__ == "__main__":
    main()
