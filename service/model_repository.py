# Link 模型仓库模块，发现模型并安全导入按协议隔离的 V3 和 V4 部署包

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Mapping

from model_protocols import inspect_model_checkpoint
from model_protocols.registry import inspect_onnx_artifact
from model_protocols.v3.constants import REQUIRED_ASSET_FILENAMES
from model_protocols.v3.semantic_assets import validate_semantic_bundle
from training_validation import validate_model_prefix
from service.package_content import (MAX_CONTENT_FILES, MAX_CONTENT_MANIFEST_BYTES, check_path_collisions,
    content_records, install_content, path_key, validate_content, validate_content_path)


GKG_FORMAT_VERSION = 2
MODEL_PROTOCOL_VERSION = 3
MAX_PACKAGE_BYTES = 64 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 256
MAX_MEMBER_BYTES = 32 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
SAFE_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
SAFE_PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
MODEL_SUFFIXES = (".artifacts.json", ".onnx.data", ".onnx", ".pth")
ASSET_FILENAMES = {
    "knowledge_base.json",
    "hash_mapping_report.json",
    "code_embeddings.npy",
    "code_embeddings_idx.json",
    "meta_staples.json",
    "cards.cdb",
    "card_vocab.json",
    "semantic_lookup_v1.npz",
    "semantic_lookup_v1.json",
    "code_embeddings_meta.json",
    "semantic_compatibility_v1.json",
}


# 校验跨平台安全的单层文件名
def validate_safe_filename(
    filename: str,
    *,
    allowed_suffixes: tuple[str, ...] | None = None,
) -> str:
    if not isinstance(filename, str) or not filename:
        raise ValueError("文件名不能为空")
    if filename != Path(filename).name or "/" in filename or "\\" in filename:
        raise ValueError("文件名不能包含路径")
    if ".." in filename or not SAFE_FILENAME_PATTERN.fullmatch(filename):
        raise ValueError("文件名包含不支持的字符")
    if len(filename.encode("utf-8")) > 200:
        raise ValueError("文件名超过 200 字节限制")
    if filename.split(".", 1)[0].upper() in WINDOWS_RESERVED_FILENAMES:
        raise ValueError("不允许使用系统保留文件名")
    if allowed_suffixes and not any(
        filename.casefold().endswith(suffix.casefold())
        for suffix in allowed_suffixes
    ):
        raise ValueError("文件扩展名不受支持")
    return filename


# 校验 GKG 上传文件名
def validate_gkg_filename(filename: str) -> str:
    return validate_safe_filename(filename, allowed_suffixes=(".gkg",))


# 校验 GKG 清单中的跨平台安全包名
def validate_package_name(package_name: str) -> str:
    if (
        not isinstance(package_name, str)
        or not SAFE_PACKAGE_NAME_PATTERN.fullmatch(package_name)
    ):
        raise ValueError("GKG 包名不符合跨平台文件名规则")
    if (
        ".." in package_name
        or package_name.split(".", 1)[0].upper() in WINDOWS_RESERVED_FILENAMES
    ):
        raise ValueError("GKG 包名不安全")
    return package_name


# 读取并校验模型轮次制品清单
def read_artifact_manifest(path: Path) -> dict[str, Any]:
    validate_safe_filename(path.name, allowed_suffixes=(".artifacts.json",))
    if path.is_symlink() or not path.is_file():
        raise ValueError("模型制品清单必须是普通文件")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("模型制品清单超过大小限制")
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("模型制品清单必须是 JSON 对象")
    protocol = payload.get("model_protocol_version")
    if protocol == 4:
        from model_protocols.v4.backend import inspect_v4_metadata
        inspect_v4_metadata(payload)
    if (payload.get("artifact_manifest_version"), payload.get("checkpoint_format_version"), protocol) not in {(2, 2, 3), (3, 3, 4)}:
        raise ValueError("模型制品清单版本不受支持")
    model_id = payload.get("model_id")
    if not isinstance(model_id, str) or str(uuid.UUID(model_id)) != model_id:
        raise ValueError("模型 UUID 无效")
    validate_model_prefix(payload.get("model_prefix"))
    iteration = payload.get("iteration")
    if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
        raise ValueError("模型轮次无效")
    return payload


# 计算文件摘要用于避免覆盖不同内容的同名模型
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# 原子安装单个文件且拒绝覆盖不同内容
def install_file_without_identity_collision(source: Path, target: Path) -> bool:
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise PermissionError(f"目标不是普通文件: {target.name}")
        if source.stat().st_size == target.stat().st_size:
            if sha256_file(source) == sha256_file(target):
                return False
        raise PermissionError(f"拒绝覆盖内容不同的同名文件: {target.name}")
    temporary_path = None
    try:
        with source.open("rb") as source_stream, tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".import.tmp",
            dir=target.parent,
            delete=False,
        ) as target_stream:
            temporary_path = Path(target_stream.name)
            shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return True


# 校验 GKG 携带的 YGOPro 卡片数据库结构和基本内容
def validate_card_database(path: Path) -> dict[str, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("cards.cdb 必须是普通文件")
    if not 1024 <= path.stat().st_size <= 512 * 1024 * 1024:
        raise ValueError("cards.cdb 文件大小无效")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"datas", "texts"}.issubset(tables):
            raise ValueError("cards.cdb 缺少 datas 或 texts 表")
        card_count = int(connection.execute("SELECT COUNT(*) FROM datas").fetchone()[0])
        text_count = int(connection.execute("SELECT COUNT(*) FROM texts").fetchone()[0])
        if card_count < 1 or text_count < 1:
            raise ValueError("cards.cdb 不包含卡片数据")
        return {"card_count": card_count, "text_count": text_count}
    finally:
        connection.close()


# 使用同目录临时文件原子更新可变运行时资产
def replace_runtime_asset(source: Path, target: Path) -> bool:
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise PermissionError(f"资产目标不是普通文件: {target.name}")
        if source.stat().st_size == target.stat().st_size:
            if sha256_file(source) == sha256_file(target):
                return False
    temporary_path = None
    try:
        with source.open("rb") as source_stream, tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".asset.tmp",
            dir=target.parent,
            delete=False,
        ) as target_stream:
            temporary_path = Path(target_stream.name)
            shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return True


class LinkModelRepository:
    # 初始化项目内模型、资产和部署包目录
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.model_root = self.project_root / "models"
        self.asset_root = self.project_root / "model_assets" / "v3"
        self.package_root = self.project_root / "deploy_packages"
        self.model_root.mkdir(parents=True, exist_ok=True)
        self.asset_root.mkdir(parents=True, exist_ok=True)
        self.package_root.mkdir(parents=True, exist_ok=True)

    # 发现模型仓库并返回选择界面所需的公开元数据
    def discover(self, selected_path: str = "") -> dict[str, Any]:
        selected_name = Path(selected_path).name if selected_path else ""
        models = []
        invalid = []
        primary_paths = sorted(
            (
                path
                for path in self.model_root.iterdir()
                if path.is_file() and path.suffix.casefold() in {".pth", ".onnx"}
            ),
            key=lambda path: path.name.casefold(),
        )
        for path in primary_paths:
            try:
                record = self._describe_model(path)
                record["selected"] = path.name == selected_name
                models.append(record)
            except Exception as error:
                invalid.append({"file": path.name, "error": str(error)})
        models.sort(
            key=lambda item: (
                str(item["model_prefix"]).casefold(),
                int(item["iteration"]),
                str(item["format"]),
            ),
            reverse=True,
        )
        return {
            "schema_version": "galatea.link.model_catalog.v1",
            "supported_model_protocols": [3, 4],
            "models": models,
            "invalid": invalid,
        }

    # 返回专用目录中可供本地导入的 GKG 包
    def list_packages(self) -> list[dict[str, Any]]:
        packages = []
        for path in sorted(self.package_root.glob("*.gkg"), key=lambda item: item.name):
            try:
                validate_gkg_filename(path.name)
                if path.is_symlink() or not path.is_file():
                    continue
                packages.append({"filename": path.name, "size_bytes": path.stat().st_size})
            except ValueError:
                continue
        return packages

    # 将专用部署目录中的文件名解析为受限 GKG 路径
    def resolve_package(self, filename: str) -> Path:
        safe_name = validate_gkg_filename(filename)
        path = self.package_root / safe_name
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"GKG 部署包不存在: {safe_name}")
        return path

    # 根据安全文件名查找可选择的模型记录
    def get_model(self, filename: str) -> dict[str, Any]:
        safe_name = validate_safe_filename(
            filename,
            allowed_suffixes=(".pth", ".onnx"),
        )
        path = self.model_root / safe_name
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"模型不存在: {safe_name}")
        return self._describe_model(path)

    # 导入已经上传或放入专用目录的 GKG 包
    def import_gkg(
        self,
        package_path: str | Path,
        package_name: str,
        target_model_id: str | None = None,
    ) -> dict[str, Any]:
        # 导入模型包或将纯资产包绑定到指定模型池
        validate_gkg_filename(package_name)
        if target_model_id is not None:
            target_model_id = str(uuid.UUID(str(target_model_id)))
        source = Path(package_path)
        if source.is_symlink() or not source.is_file():
            raise ValueError("GKG 部署包必须是普通文件")
        if source.stat().st_size <= 0 or source.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("GKG 部署包大小无效")

        with tempfile.TemporaryDirectory(
            prefix=".gkg_import_",
            dir=self.package_root,
        ) as stage_directory:
            stage_root = Path(stage_directory)
            with zipfile.ZipFile(source, "r") as archive:
                self._safe_extract(archive, stage_root)
            validated = self._validate_stage(stage_root)
            compatibility_warnings = []
            frozen_pool_only = False
            if validated["protocol"] == 4 and not (validated["model_id"] or target_model_id):
                raise ValueError("V4 纯资产包必须明确绑定目标模型 UUID")
            if validated["protocol"] == 4:
                from model_protocols.v4.assets import load_v4_assets
                staged_assets = load_v4_assets(stage_root)
                target = self.project_root / "model_assets" / "v4" / (validated["model_id"] or target_model_id)
                pool_models = [item for item in self.discover()["models"] if item.get("model_protocol_version") == 4
                    and item.get("model_id") == (validated["model_id"] or target_model_id)]
                frozen_pool_only = not any(item.get("format") == "pytorch_checkpoint" for item in pool_models)
                if (target / "card_vocab.json").is_file():
                    try:
                        self._merge_v4_compatibility(stage_root, target, validated)
                    except ValueError:
                        # 纯冻结图不依赖外部语义标签证据，保留新包自洽的目录即可
                        if not frozen_pool_only:
                            raise
                    staged_assets.validate(load_v4_assets(target).identity, "已安装 V4 资产前缀",
                        allow_semantic_updates=not frozen_pool_only, frozen_onnx=frozen_pool_only)
                for inspected in [*pool_models, *validated["records"]]:
                    if inspected.get("format") == "onnx":
                        compatibility = staged_assets.onnx_compatibility(inspected)
                        if compatibility["warning"]:
                            compatibility_warnings.append({"primary": inspected.get("primary"), **compatibility})
            installed_files = self._install_models(
                stage_root,
                validated["manifest"]["model_files_included"],
            )
            assets_path = self._install_assets(
                stage_root,
                validated["model_id"] or target_model_id,
                validated["asset_files"],
                protocol=validated["protocol"],
                frozen_pool_only=frozen_pool_only,
            )
            content = install_content(stage_root, self.package_root, validated["manifest"], validated["content_files"])
        return {
            "package_name": package_name,
            "package_format_version": validated["manifest"]["package_format_version"],
            "model_protocol_version": validated["protocol"],
            "model_id": validated["model_id"],
            "asset_target_model_id": validated["model_id"] or target_model_id,
            "models": validated["records"],
            "installed_files": installed_files,
            "installed_asset_files": list(validated["asset_files"]),
            "assets_path": assets_path,
            "warnings": compatibility_warnings,
            "content": content,
        }

    # 在暂存区合并兼容历史并保留已验证的旧资产来源，不修改输入部署包
    def _merge_v4_compatibility(self, stage_root, target, validated):
        from model_protocols.v4.assets import load_v4_assets
        from model_protocols.v4.semantic_compatibility import (
            SEMANTIC_COMPATIBILITY_FILENAME, read_semantic_compatibility_registry,
            merge_semantic_compatibility_registries, semantic_compatibility_descriptor,
        )
        registry = merge_semantic_compatibility_registries(
            read_semantic_compatibility_registry(target), read_semantic_compatibility_registry(stage_root))
        for directory in (target, stage_root):
            assets = load_v4_assets(directory)
            descriptor = semantic_compatibility_descriptor(assets.lookup)
            bases = {descriptor["semantic_lookup_hash"]: descriptor} if descriptor else {}
            registry = merge_semantic_compatibility_registries(registry, {"format_version": 1, "bases": bases,
                "category_to_index": assets.lookup.category_to_index,
                "requirement_to_index": assets.lookup.requirement_to_index})
        if registry and registry["bases"]:
            (stage_root / SEMANTIC_COMPATIBILITY_FILENAME).write_text(
                json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
            validated["asset_files"] = sorted(set(validated["asset_files"]) | {SEMANTIC_COMPATIBILITY_FILENAME})

    # 从制品清单或模型本体读取单个模型身份
    def _describe_model(self, path: Path) -> dict[str, Any]:
        marker_path = path.with_suffix(".artifacts.json")
        if marker_path.is_file():
            manifest = read_artifact_manifest(marker_path)
            if path.suffix.casefold() == ".onnx":
                inspected = inspect_onnx_artifact(path)
                artifact = manifest.get("onnx") or {}
                if artifact.get("status") != "complete":
                    raise ValueError("ONNX 制品未标记为完整")
                files = list(artifact.get("files") or [])
            else:
                artifact = manifest.get("checkpoint") or {}
                if artifact.get("status") != "complete" or artifact.get("file") != path.name:
                    raise ValueError("PTH 制品清单未标记为完整")
                inspected = {
                    key: manifest[key]
                    for key in (
                        "checkpoint_format_version",
                        "model_protocol_version",
                        "model_id",
                        "model_prefix",
                        "iteration",
                        "run_id",
                    )
                }
                files = [path.name]
        elif path.suffix.casefold() == ".onnx":
            inspected = inspect_onnx_artifact(path)
            files = [path.name]
        else:
            inspected = inspect_model_checkpoint(path)
            files = [path.name]

        protocol = int(inspected["model_protocol_version"])
        if protocol not in {3, 4}:
            raise ValueError("模型协议版本当前未启用")
        if protocol == 4 and path.suffix.casefold() == ".pth":
            actual = inspect_model_checkpoint(path)
            for key in ("model_id", "model_prefix", "iteration", "model_protocol_version"):
                if actual.get(key) != inspected.get(key):
                    raise ValueError(f"V4 PTH 本体与制品清单不一致: {key}")
            inspected = actual
        assets_path = self._assets_for_model(str(inspected["model_id"]), protocol=protocol)
        result = {
            "primary": path.name,
            "format": "onnx" if path.suffix.casefold() == ".onnx" else "pytorch_checkpoint",
            "model_id": inspected["model_id"],
            "model_prefix": inspected["model_prefix"],
            "iteration": inspected["iteration"],
            "model_protocol_version": inspected["model_protocol_version"],
            "checkpoint_format_version": inspected.get("checkpoint_format_version"),
            "run_id": inspected.get("run_id"),
            "files": files,
            "size_bytes": sum(
                (self.model_root / name).stat().st_size
                for name in files
                if (self.model_root / name).is_file()
            ),
            "assets_path": assets_path,
            "assets_available": bool(assets_path),
        }
        if protocol == 4:
            from model_protocols.v4.assets import PROTOCOL_IDENTITY_KEYS
            result.update({key: inspected.get(key) for key in PROTOCOL_IDENTITY_KEYS})
            result.update({"online_inference_ready": False, "online_block_reason": "Link V4 在线历史与状态编码待验收", "training_stage": inspected.get("training_stage")})
        return result

    # 返回模型专属资产或兼容旧布局的公共 V3 资产
    def _assets_for_model(self, model_id: str, protocol: int = 3) -> str | None:
        if protocol == 4:
            from model_protocols.v4 import REQUIRED_ASSET_FILENAMES as required
            candidates = [self.project_root / "model_assets" / "v4" / model_id]
        else:
            required = REQUIRED_ASSET_FILENAMES
            candidates = [self.asset_root / model_id, self.asset_root]
        for candidate in candidates:
            if all((candidate / name).is_file() for name in required):
                return f"./{candidate.relative_to(self.project_root).as_posix()}"
        return None

    # 限制 ZIP 成员类型、路径、大小和压缩比后流式解压
    def _safe_extract(self, archive: zipfile.ZipFile, target_root: Path) -> None:
        members = archive.infolist()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS + MAX_CONTENT_FILES:
            raise ValueError("GKG 成员数量超出安全范围")
        for info in members:
            if "\\" in info.filename or any(part in ("", ".", "..") for part in info.filename.rstrip("/").split("/")):
                raise ValueError("GKG 包含不安全路径")
        # 先有界读取清单决定容器能力，再验证所有路径，旧格式仍执行原有限额
        try:
            with archive.open("manifest.json") as stream:
                raw_manifest = stream.read(MAX_CONTENT_MANIFEST_BYTES + 1)
        except KeyError as exc:
            raise ValueError("GKG 缺少有效 manifest.json") from exc
        if len(raw_manifest) > MAX_CONTENT_MANIFEST_BYTES:
            raise ValueError("GKG 清单过大")
        manifest = json.loads(raw_manifest)
        if not isinstance(manifest, dict) or type(manifest.get("package_format_version")) is not int:
            raise ValueError("GKG 清单或格式版本无效")
        version = manifest["package_format_version"]
        if version not in (2, 4, 5):
            raise ValueError("GKG 部署包格式版本不受支持")
        records = content_records(manifest)
        if version != 5 and (len(members) > MAX_ARCHIVE_MEMBERS or len(raw_manifest) > MAX_MANIFEST_BYTES):
            raise ValueError("旧 GKG 成员数量或清单大小超出范围")
        directories = {"/".join(row["path"].split("/")[:index]) for row in records
            for index in range(1, len(row["path"].split("/")))}
        seen = set()
        total_size = 0
        for info in members:
            if info.is_dir():
                if version != 5 or info.filename[:-1] not in directories:
                    raise ValueError("GKG 包含未声明目录")
            elif "/" in info.filename:
                if version != 5:
                    raise ValueError("旧 GKG 不支持附件路径")
                validate_content_path(info.filename)
            else:
                validate_safe_filename(info.filename)
            folded = path_key(info.filename)
            if folded in seen:
                raise ValueError(f"GKG 包含重复成员: {info.filename}")
            seen.add(folded)
            if info.flag_bits & 0x1:
                raise ValueError("GKG 不支持加密成员")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError("GKG 包含不支持的压缩格式")
            unix_mode = info.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            allowed_types = {0, stat.S_IFDIR} if info.is_dir() else {0, stat.S_IFREG}
            if stat.S_ISLNK(unix_mode) or file_type not in allowed_types:
                raise ValueError("GKG 不允许符号链接或特殊文件")
            if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                raise ValueError("GKG 成员大小超出安全范围")
            total_size += info.file_size
            if total_size > MAX_EXPANDED_BYTES:
                raise ValueError("GKG 展开大小超过安全上限")
            if info.file_size > 1024 * 1024:
                if info.compress_size <= 0 or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    raise ValueError("GKG 成员压缩比异常")

        if sum("/" not in info.filename for info in members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("GKG 根目录成员数量超出安全范围")
        check_path_collisions(info.filename for info in members if not info.is_dir())

        created = []
        try:
            for info in members:
                if info.is_dir():
                    continue
                destination = target_root / info.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info, "r") as source, destination.open("xb") as target:
                    created.append(destination)
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size or written > MAX_MEMBER_BYTES:
                            raise ValueError("GKG 成员实际大小超出声明")
                        target.write(chunk)
                if written != info.file_size:
                    raise ValueError("GKG 成员实际大小与声明不一致")
        except Exception:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise

    # 校验解包后的 GKG 清单、模型身份和语义资产
    def _validate_stage(self, stage_root: Path) -> dict[str, Any]:
        manifest_path = stage_root / "manifest.json"
        if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_CONTENT_MANIFEST_BYTES:
            raise ValueError("GKG 缺少有效 manifest.json")
        with manifest_path.open("r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, Mapping):
            raise ValueError("GKG 清单必须是 JSON 对象")
        package_version = manifest.get("package_format_version")
        if type(package_version) is not int:
            raise ValueError("GKG 格式版本必须是整数")
        package_protocol = 3 if package_version == 2 else 4 if package_version in (4, 5) else None
        if package_protocol is None:
            raise ValueError("GKG 部署包格式版本不受支持")
        if package_version != 5 and manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("旧 GKG 清单过大")
        attachments = validate_content(stage_root, manifest)
        v4_assets = None
        if package_protocol == 4:
            from model_protocols.v4.assets import inspect_protocol_identity, load_v4_assets
            inspect_protocol_identity(manifest)
            v4_assets = load_v4_assets(stage_root)
            v4_assets.validate(manifest, "GKG V4 清单")
        validate_package_name(manifest.get("package_name"))
        models_included = manifest.get("models_included")
        model_files = manifest.get("model_files_included")
        records = manifest.get("model_artifacts")
        if not isinstance(models_included, list):
            raise ValueError("GKG 模型列表结构无效")
        if not isinstance(model_files, list) or not isinstance(records, list):
            raise ValueError("GKG 模型清单结构无效")
        if len(models_included) != len(set(models_included)):
            raise ValueError("GKG 包含重复模型")
        if len(model_files) != len(set(model_files)):
            raise ValueError("GKG 包含重复模型文件")
        if len(records) != len(models_included):
            raise ValueError("GKG 模型身份记录数量不一致")
        for name in models_included:
            validate_safe_filename(name, allowed_suffixes=(".pth", ".onnx"))
        for name in model_files:
            validate_safe_filename(name, allowed_suffixes=MODEL_SUFFIXES)

        actual_names = {path.name for path in stage_root.iterdir() if path.is_file()}
        asset_files = sorted(actual_names.intersection(ASSET_FILENAMES))
        expected_names = {"manifest.json", *model_files, *asset_files}
        if actual_names != expected_names:
            raise ValueError("GKG 实际文件与清单不一致")
        declared_primary = {
            name for name in actual_names if name.casefold().endswith((".pth", ".onnx"))
        }
        if declared_primary != set(models_included):
            raise ValueError("GKG 主模型列表与实际文件不一致")

        normalized_records = []
        recorded_primary = set()
        recorded_model_files = set()
        model_ids = set()
        prefixes = set()
        format_iterations = set()
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("GKG 模型记录必须是对象")
            primary = record.get("primary")
            if primary not in models_included:
                raise ValueError("GKG 模型记录指向未知主文件")
            if primary in recorded_primary:
                raise ValueError("GKG 包含重复模型身份记录")
            recorded_primary.add(primary)
            model_id = record.get("model_id")
            if not isinstance(model_id, str) or str(uuid.UUID(model_id)) != model_id:
                raise ValueError("GKG 模型 UUID 无效")
            prefix = validate_model_prefix(record.get("model_prefix"))
            protocol = record.get("model_protocol_version")
            if protocol != package_protocol:
                raise ValueError("GKG 模型协议版本当前未启用")
            if v4_assets is not None:
                v4_assets.validate(dict(record), "GKG V4 模型记录", allow_semantic_updates=record.get("format") == "pytorch_checkpoint", frozen_onnx=record.get("format") == "onnx")
            iteration = record.get("iteration")
            if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
                raise ValueError("GKG 模型轮次无效")
            model_format = record.get("format")
            if model_format not in {"onnx", "pytorch_checkpoint"}:
                raise ValueError("GKG 模型格式不受支持")
            record_files = record.get("files")
            if not isinstance(record_files, list) or primary not in record_files:
                raise ValueError("GKG 模型依赖文件列表无效")
            if len(record_files) != len(set(record_files)):
                raise ValueError("GKG 模型记录包含重复依赖文件")
            for name in record_files:
                if name not in model_files:
                    raise ValueError("GKG 模型依赖未出现在文件清单")
            if record.get("status") != "complete":
                raise ValueError("GKG 模型记录未标记为完整")
            expected_format = (
                "onnx" if primary.casefold().endswith(".onnx")
                else "pytorch_checkpoint"
            )
            if model_format != expected_format:
                raise ValueError("GKG 模型格式与主文件扩展名不一致")

            described = self._describe_staged_model(stage_root / primary)
            if v4_assets is not None:
                v4_assets.validate(described, "GKG V4 模型本体", allow_semantic_updates=model_format == "pytorch_checkpoint", frozen_onnx=model_format == "onnx")
                from model_protocols.v4.assets import PROTOCOL_IDENTITY_KEYS
                if any(described.get(key) != record.get(key) for key in PROTOCOL_IDENTITY_KEYS):
                    raise ValueError("GKG V4 模型记录与本体协议身份不一致")
            for key, expected in (
                ("model_id", model_id),
                ("model_prefix", prefix),
                ("iteration", iteration),
                ("model_protocol_version", protocol),
            ):
                if described.get(key) != expected:
                    raise ValueError(f"GKG 模型本体 {key} 与部署清单不一致")
            model_ids.add(model_id)
            prefixes.add(prefix)
            claimed_files = set(record_files)
            marker_name = (stage_root / primary).with_suffix(
                ".artifacts.json"
            ).name
            marker_path = stage_root / marker_name
            if model_format == "onnx":
                artifact_manifest = read_artifact_manifest(marker_path)
                onnx_files = (artifact_manifest.get("onnx") or {}).get("files")
                if onnx_files != record_files:
                    raise ValueError("GKG ONNX 依赖与制品清单不一致")
                claimed_files.add(marker_name)
            elif record_files != [primary]:
                raise ValueError("GKG PTH 模型记录只能声明主检查点")
            if marker_path.is_file():
                artifact_manifest = read_artifact_manifest(marker_path)
                if v4_assets is not None:
                    v4_assets.validate(artifact_manifest, "GKG V4 制品清单", allow_semantic_updates=model_format == "pytorch_checkpoint", frozen_onnx=model_format == "onnx")
                    if any(artifact_manifest.get(key) != described.get(key) for key in PROTOCOL_IDENTITY_KEYS):
                        raise ValueError("GKG V4 制品清单与模型本体协议身份不一致")
                for key, expected in (
                    ("model_id", model_id),
                    ("model_prefix", prefix),
                    ("iteration", iteration),
                    ("model_protocol_version", protocol),
                ):
                    if artifact_manifest.get(key) != expected:
                        raise ValueError(f"GKG 制品清单 {key} 与模型记录不一致")
                claimed_files.add(marker_name)
            identity = (model_format, iteration)
            if identity in format_iterations:
                raise ValueError("GKG 包含重复格式和轮次")
            format_iterations.add(identity)
            normalized_records.append(dict(record))
            recorded_model_files.update(claimed_files)

        if recorded_primary != set(models_included):
            raise ValueError("GKG 模型身份记录与主模型列表不一致")
        if recorded_model_files != set(model_files):
            raise ValueError("GKG 模型文件清单包含未认领或缺失的制品")

        if model_ids and (len(model_ids) != 1 or len(prefixes) != 1):
            raise ValueError("单个 GKG 只能包含同一模型池和前缀")
        pth_iterations = {
            item["iteration"]
            for item in normalized_records
            if item["format"] == "pytorch_checkpoint"
        }
        onnx_iterations = {
            item["iteration"]
            for item in normalized_records
            if item["format"] == "onnx"
        }
        if pth_iterations and onnx_iterations and pth_iterations != onnx_iterations:
            raise ValueError("GKG 中 PTH 与 ONNX 轮次不一致")

        semantic_set = set(REQUIRED_ASSET_FILENAMES)
        present_semantics = semantic_set.intersection(asset_files)
        if present_semantics and present_semantics != semantic_set:
            raise ValueError("GKG 语义资产不完整")
        if present_semantics and package_protocol == 3:
            validate_semantic_bundle(stage_root)
        if package_protocol == 3 and {"card_vocab.json", "semantic_lookup_v1.json", "semantic_lookup_v1.npz", "semantic_compatibility_v1.json"}.intersection(asset_files):
            raise ValueError("V3 部署包不能混入 V4 运行资产")
        if package_protocol == 4:
            if manifest.get("includes_card_vocab") is not True or manifest.get("includes_static_semantics") is not True:
                raise ValueError("V4 部署包必须声明精确词表和编译语义资产")
            if "code_embeddings_meta.json" in asset_files and not present_semantics:
                raise ValueError("V4 代码来源清单需要完整源语义资产")
            if present_semantics:
                from model_protocols.v4.semantic_lookup import load_static_semantic_assets
                load_static_semantic_assets(stage_root, card_vocabulary=v4_assets.vocabulary, verify_source_files=True, verify_logical_hash=True)
        if "cards.cdb" in asset_files:
            validate_card_database(stage_root / "cards.cdb")
        if manifest.get("includes_kb") is not ("knowledge_base.json" in asset_files):
            raise ValueError("GKG 知识库标记与实际文件不一致")
        if manifest.get("includes_staples") is not ("meta_staples.json" in asset_files):
            raise ValueError("GKG 泛用卡标记与实际文件不一致")
        if manifest.get("includes_hash_mapping") is not (
            "hash_mapping_report.json" in asset_files
        ):
            raise ValueError("GKG 哈希映射标记与实际文件不一致")
        if manifest.get("includes_code_semantics") is not bool(present_semantics):
            raise ValueError("GKG 代码语义标记与实际文件不一致")
        if "hash_mapping_report.json" in asset_files and not present_semantics:
            raise ValueError("GKG 哈希映射需要完整运行时语义资产")
        if not models_included and not asset_files:
            raise ValueError("GKG 未包含模型或运行资产")
        return {
            "manifest": dict(manifest),
            "records": normalized_records,
            "model_id": next(iter(model_ids), None),
            "asset_files": asset_files,
            "protocol": package_protocol,
            "content_files": attachments,
        }

    # 读取暂存模型并核对内部身份
    def _describe_staged_model(self, path: Path) -> dict[str, Any]:
        if path.suffix.casefold() == ".onnx":
            return inspect_onnx_artifact(path)
        return inspect_model_checkpoint(path)

    # 将已经验证的模型文件安装到 Link 模型仓库
    def _install_models(self, stage_root: Path, filenames: list[str]) -> list[str]:
        installed = []
        self.model_root.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            target = self.model_root / filename
            if target.exists() and (target.is_symlink() or not target.is_file() or sha256_file(target) != sha256_file(stage_root / filename)):
                raise PermissionError(f"拒绝覆盖内容不同的同名文件: {filename}")
        for filename in filenames:
            source = stage_root / filename
            target = self.model_root / filename
            if install_file_without_identity_collision(source, target):
                installed.append(filename)
        return installed

    # 将语义卡库和 142 兜底池安装到模型 UUID 专属目录
    def _install_assets(
        self,
        stage_root: Path,
        model_id: str | None,
        filenames: list[str],
        protocol: int = 3,
        frozen_pool_only: bool = False,
    ) -> str | None:
        if not filenames:
            return None
        asset_root = self.project_root / "model_assets" / f"v{protocol}"
        target_root = asset_root / model_id if model_id else asset_root
        if protocol == 4 and target_root.exists():
            from model_protocols.v4 import REQUIRED_ASSET_FILENAMES as required
            from model_protocols.v4.assets import load_v4_assets
            if all((target_root / name).is_file() for name in required):
                old_assets = load_v4_assets(target_root)
                load_v4_assets(stage_root).validate(old_assets.identity, "已安装 V4 资产前缀",
                    allow_semantic_updates=not frozen_pool_only, frozen_onnx=frozen_pool_only)
        target_root.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            replace_runtime_asset(
                stage_root / filename,
                target_root / filename,
            )
        if protocol == 4:
            from model_protocols.v4.semantic_compatibility import SEMANTIC_COMPATIBILITY_FILENAME
            old_registry = target_root / SEMANTIC_COMPATIBILITY_FILENAME
            if frozen_pool_only and SEMANTIC_COMPATIBILITY_FILENAME not in filenames and old_registry.is_file():
                old_registry.unlink()
            from model_protocols.v4.assets import load_v4_assets
            load_v4_assets(target_root)
            return f"./{target_root.relative_to(self.project_root).as_posix()}"
        if set(REQUIRED_ASSET_FILENAMES).issubset(filenames):
            validate_semantic_bundle(target_root)
        if all((target_root / name).is_file() for name in REQUIRED_ASSET_FILENAMES):
            validate_semantic_bundle(target_root)
            return f"./{target_root.relative_to(self.project_root).as_posix()}"
        return None
