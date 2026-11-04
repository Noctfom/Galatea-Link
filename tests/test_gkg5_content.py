# GKG5 导入回归，覆盖真实模型校验附件原字节来源归档与恶意路径拒绝

from copy import deepcopy
import hashlib
import json
import stat
import zipfile

import pytest

from service.model_repository import LinkModelRepository
from service.package_content import install_content
from tests.v4_fixtures import write_v4_assets, write_v4_checkpoint


ATTACHMENTS = {
    "content/decks/测试 池/构筑.YDK": b"#main\n14558127\n#extra\n89631139\n!side\n",
    "content/replays/raw/test.core.jsonl.gz": b"raw-replay-bytes",
    "content/documents/说明.md": "[图示](images/test.svg)".encode(),
    "content/documents/images/test.svg": b"<svg/>",
    "content/presets/deployment.yaml": b"mode: llm_only\napi_key: not-a-real-key\n",
}


# 建立遵循公开格式的真实 V4 模型包，测试不绕过模型或资产身份校验
@pytest.fixture
def bundle(tmp_path):
    assets = write_v4_assets(tmp_path / "assets")
    model, metadata = write_v4_checkpoint(tmp_path, assets)
    record = {**assets.identity, "format": "pytorch_checkpoint", "model_id": metadata["model_id"],
        "model_prefix": "galatea", "iteration": 1, "primary": model.name, "files": [model.name], "status": "complete"}
    rows = []
    for path, data in ATTACHMENTS.items():
        _, category, relative = path.split("/", 2)
        rows.append({"category": category, "path": path, "relative_path": relative,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {**assets.identity, "package_format_version": 5, "package_name": "gkg5-test", "models_included": [model.name],
        "model_artifacts": [record], "model_files_included": [model.name], "includes_card_vocab": True,
        "includes_static_semantics": True, "includes_kb": False, "includes_staples": False,
        "includes_hash_mapping": False, "includes_code_semantics": False,
        "optional_content": {"format_version": 1, "files": rows}}
    return manifest, {model.name: model.read_bytes(), **{item.name: item.read_bytes() for item in assets.root.iterdir()}, **ATTACHMENTS}


# 按测试需要改变清单或 ZIP 成员，保留真实模型本体不变
def write_bundle(tmp_path, manifest, files, *, with_directories=False):
    path = tmp_path / "test.gkg"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        if with_directories:
            archive.writestr("content/", b"")
            archive.writestr("content/decks/", b"")
        for name, data in files.items():
            archive.writestr(name, data)
    return path


# 原字节和相对目录均保留，重复导入复用归档且不修改会话或卡组
@pytest.mark.parametrize("with_directories", [False, True])
def test_import_gkg5_preserves_models_and_archives_attachments(tmp_path, bundle, with_directories):
    manifest, files = bundle
    package = write_bundle(tmp_path, manifest, files, with_directories=with_directories)
    root = tmp_path / "link"
    root.mkdir()
    settings = root / "config.json"
    settings.write_text('{"mode": "hybrid"}', encoding="utf-8")
    repository = LinkModelRepository(root)
    result = repository.import_gkg(package, package.name)
    assert result["package_format_version"] == 5 and result["model_protocol_version"] == 4
    assert result["content"]["presets_applied"] is False
    assert result["installed_files"] == manifest["model_files_included"]
    archived = root / result["content"]["archive_path"]
    for path, data in ATTACHMENTS.items():
        assert (archived / path).read_bytes() == data
    provenance = json.loads((archived / "contents.json").read_text(encoding="utf-8"))
    assert provenance["files"] == manifest["optional_content"]["files"]
    assert provenance["models"][0]["model_id"] == result["model_id"]
    assert settings.read_text(encoding="utf-8") == '{"mode": "hybrid"}'
    assert not list((root / "decks").glob("*.ydk"))
    again = repository.import_gkg(package, package.name)
    assert again["installed_files"] == [] and again["content"] == result["content"]
    assert repository.discover()["models"][0]["online_inference_ready"] is False


# 附件合同失败必须发生在安装任何模型之前
@pytest.mark.parametrize("failure", ["hash", "size", "missing", "undeclared", "category", "relative", "schema", "empty", "old_version", "traversal", "backslash", "reserved", "case", "unicode", "directory_alias", "root_nested", "extension", "duplicate", "prefix", "model_identity", "assets_missing"])
def test_bad_gkg5_content_is_rejected_before_model_install(tmp_path, bundle, failure):
    manifest, files = deepcopy(bundle)
    rows = manifest["optional_content"]["files"]
    if failure == "hash":
        rows[0]["sha256"] = "0" * 64
    elif failure == "size":
        rows[0]["size"] += 1
    elif failure == "missing":
        files.pop(rows[0]["path"])
    elif failure == "undeclared":
        files["content/documents/extra.txt"] = b"extra"
    elif failure == "category":
        rows[0]["category"] = "presets"
    elif failure == "relative":
        rows[0]["relative_path"] = "other.ydk"
    elif failure == "schema":
        manifest["optional_content"]["format_version"] = 2
    elif failure == "empty":
        rows.clear()
    elif failure == "old_version":
        manifest["package_format_version"] = 4
    elif failure == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif failure == "model_identity":
        manifest["model_artifacts"][0]["semantic_lookup_hash"] = "0" * 64
    elif failure == "assets_missing":
        files.pop("semantic_lookup_v1.npz")
    elif failure in ("case", "unicode", "directory_alias", "prefix"):
        pairs = {"case": ("content/documents/A.txt", "content/documents/a.txt"),
            "unicode": ("content/documents/é.txt", "content/documents/e\u0301.txt"),
            "directory_alias": ("content/documents/Pool/A.txt", "content/documents/pool/B.txt"),
            "prefix": ("content/documents/A.txt", "content/documents/A.txt/B.txt")}[failure]
        for path in pairs:
            files[path] = b"x"
            rows.append({"path": path, "relative_path": path.split("/", 2)[2], "category": "documents", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()})
    else:
        path = {"traversal": "content/decks/../../escape.ydk", "backslash": "content/decks/..\\escape.ydk",
            "reserved": "content/decks/CON.ydk", "root_nested": "models/other.pth", "extension": "content/presets/run.py"}[failure]
        files[path] = files.pop(rows[0]["path"])
        rows[0]["path"] = path
        rows[0]["relative_path"] = path.split("/", 2)[-1]
    package = write_bundle(tmp_path, manifest, files)
    repository = LinkModelRepository(tmp_path / "link")
    with pytest.raises((ValueError, FileNotFoundError)):
        repository.import_gkg(package, package.name)
    assert not list(repository.model_root.iterdir())
    assert not (repository.package_root / "imported").exists()


# ZIP 符号链接不能作为普通附件解包
def test_gkg5_rejects_zip_symlinks(tmp_path, bundle):
    manifest, files = bundle
    package = write_bundle(tmp_path, manifest, files)
    with zipfile.ZipFile(package, "a") as archive:
        info = zipfile.ZipInfo("content/documents/link.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../../config.json")
    repository = LinkModelRepository(tmp_path / "link")
    with pytest.raises(ValueError, match="链接"):
        repository.import_gkg(package, package.name)


# 既有归档被本地改写时拒绝覆盖，原文件保持原样
def test_gkg5_conflicting_existing_archive_is_preserved(tmp_path, bundle):
    manifest, files = bundle
    package = write_bundle(tmp_path, manifest, files)
    repository = LinkModelRepository(tmp_path / "link")
    result = repository.import_gkg(package, package.name)
    path = repository.project_root / result["content"]["archive_path"] / next(iter(ATTACHMENTS))
    path.write_bytes(b"local edit")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        repository.import_gkg(package, package.name)
    assert path.read_bytes() == b"local edit"


# 附件安装失败回收本次暂存，不留下可被误认为成功的部分归档
def test_content_copy_failure_leaves_no_partial_archive(tmp_path, bundle, monkeypatch):
    manifest, _ = bundle
    stage = tmp_path / "stage"
    for path, data in ATTACHMENTS.items():
        target = stage / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # 模拟复制中断，不依赖磁盘空间不足等不稳定环境条件
    def fail_copy(*args):
        raise OSError("copy interrupted")
    monkeypatch.setattr("service.package_content.shutil.copyfile", fail_copy)
    root = tmp_path / "deploy_packages"
    with pytest.raises(OSError):
        install_content(stage, root, manifest, manifest["optional_content"]["files"])
    assert list((root / "imported").iterdir()) == []


# 浏览器上传走完整鉴权及异步管理链，未激活时不启动游戏或加载推理后端
@pytest.mark.asyncio
async def test_gkg5_http_upload_exposes_archive_contract(tmp_path, bundle):
    from aiohttp import FormData
    from aiohttp.test_utils import TestClient, TestServer
    from app_config import ServiceConfig
    from service.http_api import create_http_app
    from service.session_manager import LinkSessionManager
    manifest, files = bundle
    package = write_bundle(tmp_path, manifest, files)
    root = tmp_path / "link"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    manager = LinkSessionManager(root / "config.yaml")
    app = create_http_app(manager, ServiceConfig(api_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    async with TestClient(TestServer(app)) as client:
        capabilities = await client.get("/api/v1/capabilities", headers=headers)
        model_management = (await capabilities.json())["data"]["model_management"]
        assert model_management["gkg_package_format_versions"] == [2, 4, 5]
        assert model_management["gkg_optional_content"] == "archive_only"
        form = FormData()
        form.add_field("activate", "false")
        form.add_field("package", package.read_bytes(), filename="test.gkg", content_type="application/octet-stream")
        response = await client.post("/api/v1/models/import", data=form, headers=headers)
        body = await response.json()
        assert response.status == 201, body
        result = body["data"]
        assert result["selection"] is None
        assert result["imported"]["content"]["archive_path"]
        assert result["imported"]["content"]["presets_applied"] is False
        assert len(result["imported"]["content"]["files"]) == len(ATTACHMENTS)
        assert manager.is_running is False
