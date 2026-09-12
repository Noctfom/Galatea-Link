# Link 卡组仓库模块，发现本地卡组并接收带来源的 AstrBot YDK 副本

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from utils.card_reader import card_db
from utils.deck_utils import load_deck


SAFE_DECK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
MAX_YDK_BYTES = 512 * 1024
MAX_SOURCE_TEXT = 500
ASTRBOT_DECK_TTL_SECONDS = 24 * 60 * 60
MAX_LOCAL_DECK_NAME = 120
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


# 规范化来源文本并限制写入元数据的长度
def normalize_source_text(value: Any, label: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label} 不能为空")
    if "\x00" in text or len(text) > MAX_SOURCE_TEXT:
        raise ValueError(f"{label} 无效或过长")
    return text


# 校验 Link 本地卡组文件名并保留可读的 Unicode 名称
def validate_local_deck_filename(value: Any) -> str:
    filename = str(value or "").strip()
    if not filename or len(filename) > MAX_LOCAL_DECK_NAME:
        raise ValueError("本地卡组文件名不能为空或超过 120 个字符")
    if Path(filename).name != filename or any(
        char in '<>:"/\\|?*' or ord(char) < 32
        for char in filename
    ):
        raise ValueError("本地卡组文件名包含不允许的字符")
    if not filename.casefold().endswith(".ydk"):
        raise ValueError("本地卡组文件必须使用 .ydk 扩展名")
    stem = filename[:-4].strip()
    if not stem or stem.endswith((".", " ")):
        raise ValueError("本地卡组文件名无效")
    if stem.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError("本地卡组文件名属于系统保留名称")
    return f"{stem}.ydk"


# 将 AstrBot 原始会话标识转换为不可逆的目录作用域
def hash_scope_id(scope_id: Any) -> str:
    normalized = normalize_source_text(
        scope_id,
        "AstrBot 卡组作用域",
        required=True,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


# 解析并规范化 YDK 三个区域的卡片编号
def normalize_ydk_text(text: str) -> tuple[str, dict[str, list[int]]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("YDK 内容不能为空")
    if len(text.encode("utf-8")) > MAX_YDK_BYTES:
        raise ValueError("YDK 内容超过 512 KiB 限制")
    sections = {"main": [], "extra": [], "side": []}
    current = None
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "#main":
            current = "main"
            continue
        if line == "#extra":
            current = "extra"
            continue
        if line == "!side":
            current = "side"
            continue
        if line.startswith("#") or line.startswith("!"):
            continue
        if current is None or not line.isdigit():
            raise ValueError(f"YDK 包含无法识别的卡片行: {line[:80]}")
        code = int(line)
        if not 1 <= code <= 0xFFFFFFFF:
            raise ValueError("YDK 卡片编号超出范围")
        sections[current].append(code)
    if not sections["main"]:
        raise ValueError("YDK 主卡组不能为空")
    if len(sections["main"]) > 100:
        raise ValueError("YDK 主卡组超过 100 张安全限制")
    if len(sections["extra"]) > 30 or len(sections["side"]) > 30:
        raise ValueError("YDK 额外卡组或副卡组超过 30 张安全限制")
    canonical = ["#created by Galatea Link", "#main"]
    canonical.extend(str(code) for code in sections["main"])
    canonical.append("#extra")
    canonical.extend(str(code) for code in sections["extra"])
    canonical.append("!side")
    canonical.extend(str(code) for code in sections["side"])
    canonical.append("")
    return "\n".join(canonical), sections


# 应用一组受限的单卡增删与跨区域移动操作
def apply_deck_operations(
    sections: dict[str, list[int]],
    operations: list[Mapping[str, Any]],
) -> str:
    if not isinstance(operations, list) or not 1 <= len(operations) <= 20:
        raise ValueError("卡组修改操作数量必须位于 1 到 20")
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise ValueError("卡组修改操作必须是对象")
        unknown = set(operation) - {
            "operation",
            "code",
            "section",
            "to_section",
            "count",
        }
        if unknown:
            raise ValueError(f"卡组修改操作包含未知字段: {sorted(unknown)}")
        action = str(operation.get("operation", "")).strip().casefold()
        section = str(operation.get("section", "")).strip().casefold()
        to_section = str(operation.get("to_section", "")).strip().casefold()
        if action not in {"add", "remove", "move"}:
            raise ValueError("卡组修改 operation 必须是 add、remove 或 move")
        if section not in sections:
            raise ValueError("卡组修改 section 必须是 main、extra 或 side")
        code = operation.get("code")
        count = operation.get("count", 1)
        if isinstance(code, bool) or not isinstance(code, int) or not 1 <= code <= 0xFFFFFFFF:
            raise ValueError("卡组修改 code 必须是有效卡片编号")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 3:
            raise ValueError("单次卡组修改数量必须位于 1 到 3")
        if action == "add":
            sections[section].extend([code] * count)
            continue
        if sections[section].count(code) < count:
            raise ValueError(f"{section} 中没有足够的卡片 {code}")
        for _ in range(count):
            sections[section].remove(code)
        if action == "move":
            if to_section not in sections or to_section == section:
                raise ValueError("move 必须指定不同的有效 to_section")
            sections[to_section].extend([code] * count)
    updated_text = "#main\n" + "\n".join(str(code) for code in sections["main"])
    updated_text += "\n#extra\n" + "\n".join(str(code) for code in sections["extra"])
    updated_text += "\n!side\n" + "\n".join(str(code) for code in sections["side"])
    canonical, _ = normalize_ydk_text(updated_text)
    return canonical


# 原子写入 UTF-8 文本文件
def write_text_atomically(path: Path, text: str) -> None:
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
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


class LinkDeckRepository:
    # 初始化 Link 本地卡组和 AstrBot 专用导入目录
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.deck_root = self.project_root / "decks"
        self.astrbot_root = self.deck_root / "astrbot_imports"
        self.deck_root.mkdir(parents=True, exist_ok=True)
        self.astrbot_root.mkdir(parents=True, exist_ok=True)

    # 返回全部卡组名称、内容摘要和来源信息
    def discover(self, scope_id: str | None = None) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.deck_root.glob("*.ydk"), key=lambda item: item.name.casefold()):
            if path.is_file() and not path.is_symlink():
                records.append(
                    self._describe(
                        path,
                        path.stem,
                        path.stem,
                        {
                            "kind": "link_local",
                            "label": "Link 本地卡组",
                            "filename": path.name,
                        },
                    )
                )
        if scope_id is None:
            return records
        scope_hash = hash_scope_id(scope_id)
        scope_root = self.astrbot_root / scope_hash
        self._cleanup_scope(scope_root)
        for path in sorted(scope_root.glob("*.ydk"), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.is_symlink():
                continue
            deck_id = path.stem
            if not SAFE_DECK_ID_PATTERN.fullmatch(deck_id):
                continue
            metadata_path = path.with_suffix(".meta.json")
            try:
                with metadata_path.open("r", encoding="utf-8") as stream:
                    metadata = json.load(stream)
                if not isinstance(metadata, Mapping):
                    continue
                if metadata.get("scope_hash") != scope_hash:
                    continue
                display_name = normalize_source_text(
                    metadata.get("display_name"),
                    "卡组显示名",
                    required=True,
                )
                source = metadata.get("source")
                if not isinstance(source, Mapping):
                    continue
                records.append(
                    self._describe(
                        path,
                        f"astrbot:{scope_hash}:{deck_id}",
                        display_name,
                        dict(source),
                    )
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        records.sort(
            key=lambda item: (
                item["source"].get("kind") not in {
                    "astrbot_toolbox",
                    "link_local_copy",
                },
                item["display_name"].casefold(),
            )
        )
        return records

    # 将浏览器提供的 YDK 保存为 Link 根目录本地卡组
    def import_local_ydk(
        self,
        filename: Any,
        ydk_text: Any,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        normalized_filename = validate_local_deck_filename(filename)
        if not isinstance(ydk_text, str):
            raise ValueError("YDK 内容必须是文本")
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite 必须是布尔值")
        canonical, _ = normalize_ydk_text(ydk_text)
        target = self.deck_root / normalized_filename
        if target.is_symlink():
            raise PermissionError("不允许覆盖符号链接卡组")
        if target.exists() and not overwrite:
            raise FileExistsError("同名本地卡组已经存在，请确认后启用覆盖")
        write_text_atomically(target, canonical)
        deck_ref = target.stem
        return self._describe(
            target,
            deck_ref,
            deck_ref,
            {
                "kind": "link_local",
                "label": "Link 本地卡组",
                "filename": target.name,
            },
        )

    # 删除 Link 根目录中的指定本地卡组文件
    def delete_local_deck(self, deck_ref: Any) -> dict[str, Any]:
        normalized_ref = normalize_source_text(
            deck_ref,
            "Link 本地卡组引用",
            required=True,
        )
        filename = validate_local_deck_filename(f"{normalized_ref}.ydk")
        target = self.deck_root / filename
        if target.is_symlink():
            raise PermissionError("不允许删除符号链接卡组")
        if not target.is_file():
            raise FileNotFoundError("指定的 Link 本地卡组不存在")
        record = self._describe(
            target,
            normalized_ref,
            normalized_ref,
            {
                "kind": "link_local",
                "label": "Link 本地卡组",
                "filename": target.name,
            },
        )
        target.unlink()
        return record

    # 按单卡操作修改 Link 根目录中的本地卡组
    def edit_local_deck(
        self,
        deck_ref: Any,
        operations: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_ref = normalize_source_text(
            deck_ref,
            "Link 本地卡组引用",
            required=True,
        )
        filename = validate_local_deck_filename(f"{normalized_ref}.ydk")
        deck_path = self.deck_root / filename
        if deck_path.is_symlink():
            raise PermissionError("不允许修改符号链接卡组")
        if not deck_path.is_file():
            raise FileNotFoundError("指定的 Link 本地卡组不存在")
        with deck_path.open("r", encoding="utf-8-sig") as stream:
            _, sections = normalize_ydk_text(stream.read())
        canonical = apply_deck_operations(sections, operations)
        write_text_atomically(deck_path, canonical)
        return self._describe(
            deck_path,
            normalized_ref,
            normalized_ref,
            {
                "kind": "link_local",
                "label": "Link 本地卡组",
                "filename": deck_path.name,
                "content_modified": True,
                "last_edited_at": time.time(),
            },
        )

    # 导入 AstrBot 工具箱缓存并返回可供配置使用的 deck_ref
    def import_astrbot_ydk(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("AstrBot 卡组导入参数必须是对象")
        display_name = normalize_source_text(
            payload.get("display_name"),
            "卡组显示名",
            required=True,
        )
        scope_hash = hash_scope_id(payload.get("scope_id"))
        instance_name = normalize_source_text(
            payload.get("instance_name", "AstrBot"),
            "AstrBot 实例名称",
            required=True,
        )
        canonical, _ = normalize_ydk_text(payload.get("ydk_text"))
        identity = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()[:20]
        deck_id = f"astrbot_{identity}"
        scope_root = self.astrbot_root / scope_hash
        self._cleanup_scope(scope_root)
        deck_path = scope_root / f"{deck_id}.ydk"
        metadata_path = deck_path.with_suffix(".meta.json")
        existing_metadata: Mapping[str, Any] = {}
        if metadata_path.is_file() and not metadata_path.is_symlink():
            try:
                loaded_metadata = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
                if isinstance(loaded_metadata, Mapping):
                    existing_metadata = loaded_metadata
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if display_name == "当前会话工具箱缓存":
            existing_name = existing_metadata.get("display_name")
            if isinstance(existing_name, str) and existing_name.strip():
                display_name = existing_name.strip()
        imported_at = time.time()
        source = {
            "kind": "astrbot_toolbox",
            "label": "当前 AstrBot 会话的工具箱缓存",
            "instance_name": instance_name,
            "scope": "current_session",
            "imported_at": imported_at,
            "expires_at": imported_at + ASTRBOT_DECK_TTL_SECONDS,
        }
        existing_source = existing_metadata.get("source")
        if (
            deck_path.is_file()
            and not deck_path.is_symlink()
            and isinstance(existing_source, Mapping)
            and existing_source.get("content_modified") is True
        ):
            source = dict(existing_source)
            source["instance_name"] = instance_name
            source["last_source_sync_at"] = imported_at
            source["expires_at"] = imported_at + ASTRBOT_DECK_TTL_SECONDS
            write_text_atomically(
                metadata_path,
                json.dumps(
                    {
                        "schema_version": "galatea.link.deck_source.v1",
                        "scope_hash": scope_hash,
                        "display_name": display_name,
                        "source": source,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            return self._describe(
                deck_path,
                f"astrbot:{scope_hash}:{deck_id}",
                display_name,
                source,
            )
        write_text_atomically(deck_path, canonical)
        write_text_atomically(
            metadata_path,
            json.dumps(
                {
                    "schema_version": "galatea.link.deck_source.v1",
                    "scope_hash": scope_hash,
                    "display_name": display_name,
                    "source": source,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return self._describe(
            deck_path,
            f"astrbot:{scope_hash}:{deck_id}",
            display_name,
            source,
        )

    # 将 Link 本地卡组复制为当前 AstrBot 会话可编辑的临时副本
    def copy_local_to_astrbot(
        self,
        scope_id: str,
        deck_ref: str,
        instance_name: str = "AstrBot",
    ) -> dict[str, Any]:
        normalized_ref = normalize_source_text(
            deck_ref,
            "Link 本地卡组引用",
            required=True,
        )
        local_record = next(
            (
                item
                for item in self.discover()
                if item["deck_ref"] == normalized_ref
                and item["source"].get("kind") == "link_local"
            ),
            None,
        )
        if local_record is None:
            raise FileNotFoundError("指定的 Link 本地卡组不存在")
        source_path = self.deck_root / local_record["source"]["filename"]
        if not source_path.is_file() or source_path.is_symlink():
            raise FileNotFoundError("Link 本地卡组文件不可用")
        with source_path.open("r", encoding="utf-8-sig") as stream:
            canonical, _ = normalize_ydk_text(stream.read())
        scope_hash = hash_scope_id(scope_id)
        normalized_instance = normalize_source_text(
            instance_name,
            "AstrBot 实例名称",
            required=True,
        )
        identity = hashlib.sha256(
            ("link_local_copy\0" + normalized_ref + "\0" + canonical).encode("utf-8")
        ).hexdigest()[:20]
        deck_id = f"astrbot_{identity}"
        scope_root = self.astrbot_root / scope_hash
        self._cleanup_scope(scope_root)
        deck_path = scope_root / f"{deck_id}.ydk"
        metadata_path = deck_path.with_suffix(".meta.json")
        existing_metadata: Mapping[str, Any] = {}
        if metadata_path.is_file() and not metadata_path.is_symlink():
            try:
                loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    existing_metadata = loaded
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        copied_at = time.time()
        source = {
            "kind": "link_local_copy",
            "label": "Link 本地卡组的当前会话临时副本",
            "instance_name": normalized_instance,
            "scope": "current_session",
            "origin_display_name": local_record["display_name"],
            "imported_at": copied_at,
            "expires_at": copied_at + ASTRBOT_DECK_TTL_SECONDS,
        }
        existing_source = existing_metadata.get("source")
        if (
            deck_path.is_file()
            and not deck_path.is_symlink()
            and isinstance(existing_source, Mapping)
            and existing_source.get("content_modified") is True
        ):
            source = dict(existing_source)
            source["instance_name"] = normalized_instance
            source["last_source_sync_at"] = copied_at
            source["expires_at"] = copied_at + ASTRBOT_DECK_TTL_SECONDS
        else:
            write_text_atomically(deck_path, canonical)
        display_name = f"临时副本 · {local_record['display_name']}"
        write_text_atomically(
            metadata_path,
            json.dumps(
                {
                    "schema_version": "galatea.link.deck_source.v1",
                    "scope_hash": scope_hash,
                    "display_name": display_name,
                    "source": source,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return self._describe(
            deck_path,
            f"astrbot:{scope_hash}:{deck_id}",
            display_name,
            source,
        )

    # 仅清理当前会话目录中超过工具箱缓存寿命的卡组副本
    def _cleanup_scope(self, scope_root: Path) -> None:
        if not scope_root.is_dir() or scope_root.is_symlink():
            return
        now = time.time()
        for metadata_path in scope_root.glob("*.meta.json"):
            if not metadata_path.is_file() or metadata_path.is_symlink():
                continue
            try:
                with metadata_path.open("r", encoding="utf-8") as stream:
                    metadata = json.load(stream)
                source = metadata.get("source", {})
                expires_at = float(source.get("expires_at", 0))
                if expires_at <= 0 or expires_at > now:
                    continue
                deck_id = metadata_path.name.removesuffix(".meta.json")
                if not SAFE_DECK_ID_PATTERN.fullmatch(deck_id):
                    continue
                deck_path = scope_root / f"{deck_id}.ydk"
                metadata_path.unlink()
                if deck_path.is_file() and not deck_path.is_symlink():
                    deck_path.unlink()
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue

    # 判断 deck_ref 是否对应当前仓库中的可用卡组
    def contains(self, deck_ref: str, scope_id: str | None = None) -> bool:
        return any(
            item["deck_ref"] == deck_ref
            for item in self.discover(scope_id=scope_id)
        )

    # 按当前会话作用域修改已导入的 AstrBot 临时卡组
    def edit_astrbot_deck(
        self,
        scope_id: str,
        deck_ref: str,
        operations: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        scope_hash = hash_scope_id(scope_id)
        parts = str(deck_ref).split(":")
        if len(parts) != 3 or parts[0] != "astrbot" or parts[1] != scope_hash:
            raise PermissionError("只能修改当前 AstrBot 会话导入的临时卡组")
        deck_id = parts[2]
        if not SAFE_DECK_ID_PATTERN.fullmatch(deck_id):
            raise ValueError("临时卡组引用无效")
        scope_root = self.astrbot_root / scope_hash
        self._cleanup_scope(scope_root)
        deck_path = scope_root / f"{deck_id}.ydk"
        metadata_path = deck_path.with_suffix(".meta.json")
        if not deck_path.is_file() or deck_path.is_symlink():
            raise FileNotFoundError("当前临时卡组不存在或已经过期")
        if not metadata_path.is_file() or metadata_path.is_symlink():
            raise ValueError("当前临时卡组缺少来源元数据")
        with deck_path.open("r", encoding="utf-8-sig") as stream:
            _, sections = normalize_ydk_text(stream.read())
        canonical = apply_deck_operations(sections, operations)
        with metadata_path.open("r", encoding="utf-8") as stream:
            metadata = json.load(stream)
        if not isinstance(metadata, Mapping) or metadata.get("scope_hash") != scope_hash:
            raise ValueError("当前临时卡组来源元数据无效")
        metadata = dict(metadata)
        source = dict(metadata.get("source") or {})
        edited_at = time.time()
        source["modified_by"] = "AstrBot 主智能体"
        source["content_modified"] = True
        source["last_edited_at"] = edited_at
        source["expires_at"] = edited_at + ASTRBOT_DECK_TTL_SECONDS
        metadata["source"] = source
        write_text_atomically(deck_path, canonical)
        write_text_atomically(
            metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
        return self._describe(
            deck_path,
            deck_ref,
            str(metadata["display_name"]),
            source,
        )

    # 将卡组实体压缩为带中文名称和投入数量的公开记录
    def _describe(
        self,
        path: Path,
        deck_ref: str,
        display_name: str,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        deck = load_deck(self.deck_root, deck_ref)
        if deck is None:
            raise ValueError(f"无法读取卡组: {deck_ref}")
        sections = {}
        for section_name, codes in (
            ("main", deck.main),
            ("extra", deck.extra),
            ("side", deck.side),
        ):
            counter = Counter(int(code) for code in codes)
            sections[section_name] = [
                {
                    "code": code,
                    "name": card_db.get_card_name(code),
                    "count": count,
                }
                for code, count in counter.items()
            ]
        return {
            "schema_version": "galatea.link.deck.v1",
            "deck_ref": deck_ref,
            "display_name": display_name,
            "source": source,
            "counts": {
                "main": len(deck.main),
                "extra": len(deck.extra),
                "side": len(deck.side),
            },
            "cards": sections,
            "updated_at": path.stat().st_mtime,
        }
