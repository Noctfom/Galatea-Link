# GKG5 附件合同与独立归档，核验原始字节且不解释或应用预设内容

import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unicodedata


MAX_CONTENT_FILES = 16384
MAX_CONTENT_MANIFEST_BYTES = 32 * 1024 * 1024
CONTENT_SUFFIXES = {
    "decks": (".ydk",),
    "replays": (".json", ".jsonl", ".jsonl.gz", ".yrp", ".yrp2", ".yrp3", ".yrp3d", ".yrpx"),
    "documents": (".md", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"),
    "presets": (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt"),
}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


# 使用跨平台路径身份，预先发现大小写和 Unicode 等价冲突
def path_key(value):
    return unicodedata.normalize("NFC", value).casefold()


# 附件只能位于专用分类，中文和空格可以保留但目录不能逃出归档位置
def validate_content_path(value):
    if not isinstance(value, str) or not value or "\\" in value or len(value.encode("utf-8")) > 1024:
        raise ValueError("GKG 附件路径无效")
    parts = value.split("/")
    if len(parts) < 3 or len(parts) > 16 or parts[0] != "content" or parts[1] not in CONTENT_SUFFIXES:
        raise ValueError("GKG 附件不在已知分类中")
    for part in parts:
        if (not part or part.startswith(".") or part.endswith((".", " ")) or len(part.encode("utf-8")) > 200
                or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)
                or part.split(".", 1)[0].upper() in RESERVED_NAMES):
            raise ValueError("GKG 附件路径包含不安全名称")
    if not value.casefold().endswith(CONTENT_SUFFIXES[parts[1]]):
        raise ValueError("GKG 附件扩展名不受支持")
    return parts[1], "/".join(parts[2:])


# 同时检查文件目录冲突和不同拼写指向同一路径的情况
def check_path_collisions(paths):
    nodes = {}
    for value in paths:
        parts = value.split("/")
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            key, kind = path_key(path), "file" if index == len(parts) else "directory"
            previous = nodes.get(key)
            if previous is not None and (previous != (path, kind) or kind == "file"):
                raise ValueError("GKG 附件路径重复或发生文件目录冲突")
            nodes[key] = (path, kind)


# 按容器版本读取附件清单，不将容器版本误当作模型协议
def content_records(manifest):
    if manifest["package_format_version"] != 5:
        if "optional_content" in manifest:
            raise ValueError("旧 GKG 格式不能声明附件")
        return []
    content = manifest.get("optional_content")
    if not isinstance(content, dict) or type(content.get("format_version")) is not int or content["format_version"] != 1:
        raise ValueError("GKG5 附件清单格式不受支持")
    rows = content.get("files")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_CONTENT_FILES:
        raise ValueError("GKG5 附件清单不能为空或超过数量上限")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("GKG5 附件记录必须是对象")
        category, relative = validate_content_path(row.get("path"))
        if row.get("category") != category or row.get("relative_path") != relative:
            raise ValueError("GKG5 附件分类或相对路径与清单不符")
        if type(row.get("size")) is not int or not 0 <= row["size"] <= 32 * 1024 * 1024 * 1024:
            raise ValueError("GKG5 附件大小声明无效")
        if not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise ValueError("GKG5 附件摘要声明无效")
        normalized.append({key: row[key] for key in ("category", "path", "relative_path", "size", "sha256")})
    check_path_collisions(row["path"] for row in normalized)
    return normalized


# 流式摘要不把大型重放文件整体载入内存
def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# 解包后再对实际附件集合和每个文件的字节进行核验
def validate_content(stage_root, manifest):
    rows = content_records(manifest)
    actual = {path.relative_to(stage_root).as_posix() for path in (stage_root / "content").rglob("*") if path.is_file()}
    if actual != {row["path"] for row in rows}:
        raise ValueError("GKG5 附件实际文件与清单不一致")
    for row in rows:
        source = stage_root / row["path"]
        if source.stat().st_size != row["size"] or file_digest(source) != row["sha256"]:
            raise ValueError(f"GKG5 附件大小或摘要不符: {row['path']}")
    return rows


# 归档位置及所有父目录不得经过符号链接或 Windows 目录联接
def reject_links(path):
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("GKG 附件归档不能经过链接或目录联接")


# 将已核验附件整体安装到内容隔离目录，重复导入复用原字节且不激活任何附件
def install_content(stage_root, package_root, manifest, rows):
    if not rows:
        return {"files": [], "archive_path": None, "presets_applied": False}
    identity = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    group = f"{manifest['package_name']}-{hashlib.sha256(identity).hexdigest()[:16]}"
    parent = package_root / "imported"
    target = parent / group
    reject_links(target)
    parent.mkdir(parents=True, exist_ok=True)
    provenance = {"format_version": 1, "package_name": manifest["package_name"],
        "model_protocol_version": manifest.get("model_protocol_version"), "models": manifest["model_artifacts"], "files": rows}
    provenance_text = json.dumps(provenance, ensure_ascii=False, indent=2)
    if target.exists():
        reject_links(target / "contents.json")
        actual = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
        if actual != {"contents.json", *(row["path"] for row in rows)}:
            raise ValueError("GKG 附件归档已有内容与清单冲突")
        if (target / "contents.json").read_text(encoding="utf-8") != provenance_text:
            raise ValueError("GKG 附件来源清单冲突，拒绝覆盖")
        for row in rows:
            path = target / row["path"]
            reject_links(path)
            if path.stat().st_size != row["size"] or file_digest(path) != row["sha256"]:
                raise ValueError("GKG 附件归档已有文件内容不同，拒绝覆盖")
    else:
        with tempfile.TemporaryDirectory(prefix=".content_import_", dir=parent) as temporary:
            staged = Path(temporary) / "archive"
            staged.mkdir()
            for row in rows:
                destination = staged / row["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(stage_root / row["path"], destination)
            (staged / "contents.json").write_text(provenance_text, encoding="utf-8")
            staged.rename(target)
    return {"files": rows, "archive_path": f"./{target.relative_to(package_root.parent).as_posix()}", "presets_applied": False}
