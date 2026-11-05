# GKG5 卡组显式安装模块，核验附件归档并生成有来源的本地可编辑副本

import hashlib
import json
from pathlib import Path
import re

from service.deck_repository import MAX_YDK_BYTES, normalize_ydk_text, write_text_atomically
from service.package_content import MAX_CONTENT_MANIFEST_BYTES, content_records, file_digest, reject_links


# 将已归档包中的卡组按明确选择安装，保留包内原件和已有本地修改
def install_package_decks(project_root, archive_id):
    if not isinstance(archive_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}-[0-9a-f]{16}", archive_id) or ".." in archive_id:
        raise ValueError("GKG 附件归档标识无效")
    root = Path(project_root).resolve()
    archive = root / "deploy_packages" / "imported" / archive_id
    manifest_path = archive / "contents.json"
    reject_links(manifest_path)
    if not manifest_path.is_file():
        raise FileNotFoundError("GKG 附件归档不存在，请先导入部署包")
    if manifest_path.stat().st_size > MAX_CONTENT_MANIFEST_BYTES:
        raise ValueError("GKG 附件来源清单超过大小限制")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise ValueError("GKG 附件来源清单无效")
    package_name = manifest.get("package_name")
    if not isinstance(package_name, str) or archive_id[:-17] != package_name:
        raise ValueError("GKG 附件来源与归档标识不一致")
    rows = content_records({"package_format_version": 5, "optional_content": {"format_version": 1, "files": manifest.get("files")}})
    group = hashlib.sha256(archive_id.encode("utf-8")).hexdigest()[:24]
    folder = root / "decks" / "gkg_imports" / group
    reject_links(folder)
    prepared, skipped = [], []
    for row in rows:
        if row["category"] != "decks":
            continue
        source_path = archive / row["path"]
        reject_links(source_path)
        if not source_path.is_file() or source_path.stat().st_size != row["size"] or file_digest(source_path) != row["sha256"]:
            raise ValueError(f"GKG 卡组归档缺失或摘要不符: {row['relative_path']}")
        try:
            if row["size"] > MAX_YDK_BYTES:
                raise ValueError("YDK 超过 512 KiB 限制")
            canonical, _ = normalize_ydk_text(source_path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, ValueError) as error:
            skipped.append({"path": row["relative_path"], "reason": str(error)})
            continue
        deck_id = hashlib.sha256(row["relative_path"].encode("utf-8")).hexdigest()[:24]
        target = folder / f"{deck_id}.ydk"
        metadata_path = target.with_suffix(".meta.json")
        reject_links(target)
        reject_links(metadata_path)
        source = {"kind": "link_local", "origin": "gkg_package", "label": f"GKG 包 · {package_name}",
            "filename": target.relative_to(root / "decks").as_posix(), "package_name": package_name,
            "package_archive_id": archive_id, "package_relative_path": row["relative_path"], "package_sha256": row["sha256"]}
        metadata = {"schema_version": "galatea.link.deck_source.v1", "package_group": group,
            "display_name": f"{package_name} · {Path(row['relative_path']).stem}", "source": source}
        if target.exists() or metadata_path.exists():
            if not target.is_file() or not metadata_path.is_file():
                raise ValueError("GKG 本地卡组副本或来源元数据缺失，拒绝覆盖")
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            existing_source = existing.get("source", {}) if isinstance(existing, dict) else {}
            if not isinstance(existing_source, dict) or any(existing_source.get(key) != source[key] for key in ("package_archive_id", "package_relative_path", "package_sha256")):
                raise ValueError("GKG 本地卡组已有来源冲突，拒绝覆盖")
        prepared.append((target, canonical, metadata, target.exists()))
    installed, preserved = [], []
    for target, canonical, metadata, exists in prepared:
        reference = f"gkg:{group}:{target.stem}"
        if exists:
            preserved.append(reference)
            continue
        metadata_path = target.with_suffix(".meta.json")
        try:
            write_text_atomically(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2))
            write_text_atomically(target, canonical)
        except BaseException:
            if not target.exists():
                metadata_path.unlink(missing_ok=True)
            raise
        installed.append(reference)
    return {"archive_id": archive_id, "directory": folder.relative_to(root).as_posix(),
        "installed": installed, "preserved": preserved, "skipped": skipped}
