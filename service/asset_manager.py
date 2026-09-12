# Link 运行资产管理模块，负责卡库脚本语义资产和 142 宣言兜底池

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

from model_protocols.v3.constants import REQUIRED_ASSET_FILENAMES
from model_protocols.v3.semantic_assets import (
    HASH_MAPPING_FILENAME,
    download_remote_semantic_bundle,
    validate_semantic_bundle,
)
from service.model_repository import replace_runtime_asset, validate_card_database


DEFAULT_CDB_URL = (
    "https://raw.githubusercontent.com/mycard/ygopro-database/"
    "master/locales/zh-CN/cards.cdb"
)
DEFAULT_SCRIPT_REPOSITORY = "https://github.com/Fluorohydride/ygopro-scripts.git"
DEFAULT_SEMANTIC_URL = (
    "https://raw.githubusercontent.com/Noctfom/Galatea-Core/"
    "main/knowledge_base.json"
)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_SCRIPT_FILES = 50000
MAX_SCRIPT_BYTES = 8 * 1024 * 1024
MAX_SCRIPT_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_SCRIPT_COMPRESSION_RATIO = 1000


class LinkAssetManager:
    # 初始化项目资产根目录和当前模型选择读取器
    def __init__(
        self,
        project_root: str | Path,
        selection_provider: Callable[[], Mapping[str, Any]],
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.model_asset_root = self.project_root / "model_assets" / "v3"
        self.script_root = self.project_root / "script"
        self._selection_provider = selection_provider
        self.model_asset_root.mkdir(parents=True, exist_ok=True)

    # 解析并约束当前模型选中的资产目录
    def get_active_asset_root(self) -> Path:
        selection = self._selection_provider()
        raw_path = str(selection.get("assets_path") or "model_assets/v3")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.model_asset_root)
        except ValueError as error:
            raise ValueError("模型资产目录必须位于 model_assets/v3 内") from error
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    # 返回当前卡库语义脚本和 142 兜底池状态
    def get_status(self) -> dict[str, Any]:
        asset_root = self.get_active_asset_root()
        card_path = self._resolve_card_database(asset_root)
        card_info = None
        if card_path.is_file():
            try:
                card_info = validate_card_database(card_path)
            except (OSError, ValueError, sqlite3.Error) as error:
                card_info = {"error": str(error)}
        semantic_files = {
            name: (asset_root / name).is_file()
            for name in REQUIRED_ASSET_FILENAMES
        }
        staples = self.get_meta_staples()
        script_count = (
            sum(1 for _ in self.script_root.glob("*.lua"))
            if self.script_root.is_dir()
            else 0
        )
        return {
            "schema_version": "galatea.link.assets.v1",
            "active_asset_path": f"./{asset_root.relative_to(self.project_root).as_posix()}",
            "card_database": {
                "path": f"./{card_path.relative_to(self.project_root).as_posix()}",
                "exists": card_path.is_file(),
                **(card_info or {}),
            },
            "semantic_files": semantic_files,
            "semantic_complete": all(semantic_files.values()),
            "script_count": script_count,
            "local_semantic_builder": {
                "available": (
                    importlib.util.find_spec("torch") is not None
                    and importlib.util.find_spec("sentence_transformers") is not None
                ),
                "dependency_file": "requirements-semantic.txt",
            },
            "meta_staples": staples,
            "defaults": {
                "cdb_url": DEFAULT_CDB_URL,
                "script_repository": DEFAULT_SCRIPT_REPOSITORY,
                "semantic_url": DEFAULT_SEMANTIC_URL,
            },
        }

    # 同步萌卡 CDB 和官方 Lua 脚本到 Link 自有目录
    def sync_card_data(
        self,
        *,
        cdb_url: str = DEFAULT_CDB_URL,
        script_repository: str = DEFAULT_SCRIPT_REPOSITORY,
        force_scripts: bool = False,
    ) -> dict[str, Any]:
        normalized_cdb_url = self._validate_remote_url(cdb_url)
        normalized_script_repository = self._validate_remote_url(script_repository)
        asset_root = self.get_active_asset_root()
        with tempfile.TemporaryDirectory(
            prefix=".asset_sync_",
            dir=self.project_root,
        ) as temporary_directory:
            stage_root = Path(temporary_directory)
            downloaded_cdb = stage_root / "cards.cdb"
            self._download_file(
                normalized_cdb_url,
                downloaded_cdb,
                max_bytes=512 * 1024 * 1024,
            )
            card_info = validate_card_database(downloaded_cdb)
            updated_targets = []
            for target in {
                self.project_root / "cards.cdb",
                asset_root / "cards.cdb",
            }:
                target.parent.mkdir(parents=True, exist_ok=True)
                if replace_runtime_asset(downloaded_cdb, target):
                    updated_targets.append(
                        f"./{target.relative_to(self.project_root).as_posix()}"
                    )
            script_count = self._sync_script_archive(
                normalized_script_repository,
                stage_root,
                force=bool(force_scripts),
            )
        return {
            "card_database": card_info,
            "updated_card_database_paths": sorted(updated_targets),
            "script_count": script_count,
            "restart_required": True,
        }

    # 同步并校验远程发布的完整模型协议 V3 语义资产
    def sync_semantic_bundle(
        self,
        remote_url: str = DEFAULT_SEMANTIC_URL,
    ) -> dict[str, Any]:
        normalized_url = self._validate_remote_url(remote_url)
        asset_root = self.get_active_asset_root()
        with tempfile.TemporaryDirectory(
            prefix=".semantic_sync_",
            dir=self.project_root,
        ) as temporary_directory:
            stage_root = Path(temporary_directory)
            bundle = download_remote_semantic_bundle(normalized_url, stage_root)
            if not bundle.get("installed_code_semantics"):
                raise RuntimeError(
                    "远程语义向量不完整: "
                    + json.dumps(bundle.get("errors") or {}, ensure_ascii=False)
                )
            (stage_root / "knowledge_base.json").write_text(
                json.dumps(bundle["knowledge_base"], ensure_ascii=False),
                encoding="utf-8",
            )
            if bundle.get("hash_mapping") is not None:
                (stage_root / HASH_MAPPING_FILENAME).write_text(
                    json.dumps(bundle["hash_mapping"], ensure_ascii=False),
                    encoding="utf-8",
                )
            validated = validate_semantic_bundle(stage_root)
            installed = []
            for name in (*REQUIRED_ASSET_FILENAMES, HASH_MAPPING_FILENAME):
                source = stage_root / name
                if source.is_file() and replace_runtime_asset(source, asset_root / name):
                    installed.append(name)
        return {
            "asset_path": f"./{asset_root.relative_to(self.project_root).as_posix()}",
            "installed_files": installed,
            "effect_slot_count": validated["effect_slot_count"],
            "embedding_shape": list(validated["shape"]),
            "restart_required": True,
        }

    # 在隔离暂存目录解析 Lua 并增量构建完整语义资产
    def rebuild_semantic_bundle(
        self,
        *,
        remote_url: str | None = None,
        clear_existing: bool = False,
    ) -> dict[str, Any]:
        if remote_url and clear_existing:
            raise ValueError("远端基座同步与完全本机重建不能同时启用")
        normalized_url = self._validate_remote_url(remote_url) if remote_url else None
        if not self.script_root.is_dir():
            raise FileNotFoundError("请先同步 YGOPro Lua 脚本")
        from model_protocols.v3.code_semantic_embedder import CodeSemanticEmbedder
        from model_protocols.v3.lua_semantic_parser import YGOProLuaSemanticParser

        asset_root = self.get_active_asset_root()
        with tempfile.TemporaryDirectory(
            prefix=".semantic_build_",
            dir=self.project_root,
        ) as temporary_directory:
            stage_root = Path(temporary_directory)
            if not clear_existing and normalized_url is None:
                for name in (*REQUIRED_ASSET_FILENAMES, HASH_MAPPING_FILENAME):
                    source = asset_root / name
                    if source.is_file():
                        shutil.copy2(source, stage_root / name)
            knowledge_path = stage_root / "knowledge_base.json"
            parse_result = YGOProLuaSemanticParser(self.script_root).run_batch(
                knowledge_path,
                clear_existing=bool(clear_existing),
                remote_url=normalized_url,
            )
            embedding_result = CodeSemanticEmbedder().generate_embeddings(
                knowledge_path,
                stage_root / "code_embeddings.npy",
                incremental=not clear_existing,
            )
            validated = validate_semantic_bundle(stage_root)
            installed = []
            for name in (*REQUIRED_ASSET_FILENAMES, HASH_MAPPING_FILENAME):
                source = stage_root / name
                if source.is_file() and replace_runtime_asset(source, asset_root / name):
                    installed.append(name)
        return {
            "asset_path": f"./{asset_root.relative_to(self.project_root).as_posix()}",
            "mode": (
                "full_local"
                if clear_existing
                else "remote_incremental"
                if normalized_url
                else "local_incremental"
            ),
            "installed_files": installed,
            "parse": parse_result,
            "embedding": embedding_result,
            "effect_slot_count": validated["effect_slot_count"],
            "embedding_shape": list(validated["shape"]),
            "restart_required": True,
        }

    # 返回当前模型资产优先的 142 宣言兜底池
    def get_meta_staples(self) -> list[dict[str, Any]]:
        asset_root = self.get_active_asset_root()
        staples_path = asset_root / "meta_staples.json"
        if not staples_path.is_file():
            staples_path = self.project_root / "meta_staples.json"
        codes = self._read_meta_staple_codes(staples_path)
        card_path = self._resolve_card_database(asset_root)
        names = self._read_card_names(card_path, codes)
        return [
            {"code": code, "name": names.get(code, f"Code {code}")}
            for code in codes
        ]

    # 按添加移除或替换操作更新当前模型的 142 宣言兜底池
    def update_meta_staples(
        self,
        operation: str,
        card_codes: list[Any],
    ) -> list[dict[str, Any]]:
        normalized_operation = str(operation).strip().casefold()
        if normalized_operation not in {"add", "remove", "replace"}:
            raise ValueError("142 兜底池操作只支持 add remove replace")
        normalized_codes = self._normalize_card_codes(card_codes)
        asset_root = self.get_active_asset_root()
        target = asset_root / "meta_staples.json"
        existing = self._read_meta_staple_codes(
            target if target.is_file() else self.project_root / "meta_staples.json"
        )
        if normalized_operation in {"add", "replace"}:
            names = self._read_card_names(
                self._resolve_card_database(asset_root),
                normalized_codes,
            )
            missing = [code for code in normalized_codes if code not in names]
            if missing:
                raise ValueError(f"卡片数据库中找不到卡密: {missing[:8]}")
        if normalized_operation == "replace":
            updated = normalized_codes
        elif normalized_operation == "add":
            updated = [*existing]
            updated.extend(code for code in normalized_codes if code not in updated)
        else:
            removed = set(normalized_codes)
            updated = [code for code in existing if code not in removed]
        if not updated:
            raise ValueError("142 宣言兜底池不能为空")
        self._write_json_atomically(target, updated)
        return self.get_meta_staples()

    # 优先返回模型资产携带的卡库并回退到 Link 根卡库
    def _resolve_card_database(self, asset_root: Path) -> Path:
        selected = asset_root / "cards.cdb"
        return selected if selected.is_file() else self.project_root / "cards.cdb"

    # 校验资产同步来源仅使用 HTTP 或 HTTPS
    @staticmethod
    def _validate_remote_url(url: str) -> str:
        normalized = str(url).strip()
        parts = urllib.parse.urlsplit(normalized)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("资产来源必须是有效的 HTTP 或 HTTPS 地址")
        if parts.username or parts.password:
            raise ValueError("资产来源地址不能包含用户凭据")
        return normalized

    # 在大小限制内流式下载单个远程文件
    @staticmethod
    def _download_file(url: str, target: Path, *, max_bytes: int) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "Galatea-Link/1"})
        total = 0
        with urllib.request.urlopen(request, timeout=120) as response, target.open("xb") as stream:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError("远程资产超过下载大小限制")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("远程资产超过下载大小限制")
                stream.write(chunk)
        if total < 1:
            raise ValueError("远程资产为空")

    # 将 GitHub 仓库地址转换为默认分支归档地址
    @staticmethod
    def _repository_archive_url(repository_url: str) -> str:
        match = re.fullmatch(
            r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?",
            repository_url,
            flags=re.IGNORECASE,
        )
        if match:
            owner, repository = match.groups()
            return (
                f"https://github.com/{owner}/{repository}/"
                "archive/refs/heads/master.zip"
            )
        return repository_url

    # 安全解包官方 Lua 脚本归档并替换 Link 脚本目录
    def _sync_script_archive(
        self,
        repository_url: str,
        stage_root: Path,
        *,
        force: bool,
    ) -> int:
        archive_path = stage_root / "scripts.zip"
        self._download_file(
            self._repository_archive_url(repository_url),
            archive_path,
            max_bytes=MAX_DOWNLOAD_BYTES,
        )
        staged_scripts = stage_root / "script.next"
        staged_scripts.mkdir()
        if self.script_root.is_dir() and not force:
            shutil.copytree(self.script_root, staged_scripts, dirs_exist_ok=True)
        installed = 0
        seen = set()
        expanded_bytes = 0
        with zipfile.ZipFile(archive_path, "r") as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.casefold().endswith(".lua"):
                    continue
                filename = Path(info.filename).name
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}\.lua", filename):
                    continue
                folded = filename.casefold()
                if folded in seen:
                    raise ValueError(f"脚本归档包含重复文件: {filename}")
                seen.add(folded)
                unix_mode = info.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if stat.S_ISLNK(unix_mode) or file_type not in {0, stat.S_IFREG}:
                    raise ValueError(f"脚本归档包含特殊文件: {filename}")
                expanded_bytes += info.file_size
                if (
                    len(seen) > MAX_SCRIPT_FILES
                    or info.file_size > MAX_SCRIPT_BYTES
                    or expanded_bytes > MAX_SCRIPT_EXPANDED_BYTES
                ):
                    raise ValueError("脚本归档超过安全限制")
                if (
                    info.file_size > 1024 * 1024
                    and (
                        info.compress_size <= 0
                        or info.file_size / info.compress_size
                        > MAX_SCRIPT_COMPRESSION_RATIO
                    )
                ):
                    raise ValueError(f"脚本归档压缩比异常: {filename}")
                with archive.open(info, "r") as source, (staged_scripts / filename).open("wb") as target:
                    written = 0
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size or written > MAX_SCRIPT_BYTES:
                            raise ValueError(f"脚本实际大小超过声明: {filename}")
                        target.write(chunk)
                    if written != info.file_size:
                        raise ValueError(f"脚本实际大小与声明不一致: {filename}")
                installed += 1
        if installed < 1:
            raise ValueError("脚本归档中没有可用 Lua 文件")
        backup = self.project_root / f".script.backup.{uuid.uuid4().hex}"
        try:
            if self.script_root.exists():
                os.replace(self.script_root, backup)
            os.replace(staged_scripts, self.script_root)
        except Exception:
            if backup.exists() and not self.script_root.exists():
                os.replace(backup, self.script_root)
            raise
        finally:
            if backup.exists():
                shutil.rmtree(backup)
        return sum(1 for _ in self.script_root.glob("*.lua"))

    # 读取并规范化 JSON 格式的 142 宣言兜底池
    def _read_meta_staple_codes(self, path: Path) -> list[int]:
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8-sig") as stream:
            payload = json.load(stream)
        if not isinstance(payload, list):
            raise ValueError("meta_staples.json 必须是数组")
        return self._normalize_card_codes(payload)

    # 将外部卡密列表规范化为有界去重整数列表
    @staticmethod
    def _normalize_card_codes(values: list[Any]) -> list[int]:
        if not isinstance(values, list) or len(values) > 500:
            raise ValueError("卡密列表必须是最多 500 项的数组")
        result = []
        for value in values:
            if isinstance(value, bool):
                raise ValueError("卡密不能是布尔值")
            try:
                code = int(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"无效卡密: {value!r}") from error
            if not 0 < code <= 0x0FFFFFFF:
                raise ValueError(f"卡密超出范围: {code}")
            if code not in result:
                result.append(code)
        return result

    # 从卡片数据库批量读取指定卡密的名称
    @staticmethod
    def _read_card_names(path: Path, codes: list[int]) -> dict[int, str]:
        if not codes or not path.is_file():
            return {}
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            placeholders = ",".join("?" for _ in codes)
            rows = connection.execute(
                f"SELECT id, name FROM texts WHERE id IN ({placeholders})",
                codes,
            ).fetchall()
            return {int(code): str(name) for code, name in rows}
        finally:
            connection.close()

    # 使用目标目录临时文件原子保存小型 JSON 资产
    @staticmethod
    def _write_json_atomically(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                encoding="utf-8",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
