# GKG5 导入回归，覆盖真实模型校验附件原字节来源归档与恶意路径拒绝

from copy import deepcopy
import asyncio
import hashlib
import json
import random
import stat
import zipfile

import pytest

from service.model_repository import LinkModelRepository
from service.package_content import install_content
from service.package_decks import install_package_decks
from service.deck_repository import LinkDeckRepository
from utils.deck_utils import load_deck
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
    assert repository.discover()["models"][0]["online_inference_ready"] is True


# 确认入库后可加载修改与删除，重复导入保留修改，远端临时副本保持会话隔离
def test_package_decks_opt_in_management_and_private_copies(tmp_path, bundle):
    manifest, files = bundle
    package = write_bundle(tmp_path, manifest, files)
    root = tmp_path / "link"
    imported = LinkModelRepository(root).import_gkg(package, package.name)
    repository = LinkDeckRepository(root)
    assert repository.discover() == []
    result = install_package_decks(root, imported["content"]["archive_id"])
    assert len(result["installed"]) == 1 and not result["skipped"]
    reference = result["installed"][0]
    record = repository.discover()[0]
    assert record["deck_ref"] == reference
    assert record["source"]["package_relative_path"] == "测试 池/构筑.YDK"
    deck = load_deck(repository.deck_root, reference)
    assert deck.main == [14558127] and deck.extra == [89631139]
    repository.edit_local_deck(reference, [{"operation": "add", "section": "main", "code": 14558127}])
    assert repository.discover()[0]["source"]["content_modified"] is True
    repeated = install_package_decks(root, imported["content"]["archive_id"])
    assert repeated["installed"] == [] and repeated["preserved"] == [reference]
    assert load_deck(repository.deck_root, reference).main == [14558127] * 2
    private = repository.copy_local_to_astrbot("qq:group:100", reference)
    assert private["source"]["origin_package"]["package_name"] == "gkg5-test"
    repository.edit_astrbot_deck("qq:group:100", private["deck_ref"], [{"operation": "add", "section": "side", "code": 14558127}])
    assert load_deck(repository.deck_root, reference).side == []
    assert private["deck_ref"] not in {item["deck_ref"] for item in repository.discover(scope_id="qq:group:200")}
    archived = root / imported["content"]["archive_path"]
    assert (archived / "content/decks/测试 池/构筑.YDK").read_bytes() == ATTACHMENTS["content/decks/测试 池/构筑.YDK"]
    repository.delete_local_deck(reference)
    assert repository.discover() == [] and load_deck(repository.deck_root, reference) is None
    assert (archived / "content/decks/测试 池/构筑.YDK").is_file()


# 归档摘要变化必须拒绝入库，非法 YDK 单独反馈且不影响模型导入
@pytest.mark.parametrize("failure", ["tampered", "invalid_ydk", "oversize", "traversal", "wrong_origin"])
def test_package_deck_install_rejects_invalid_sources(tmp_path, bundle, failure):
    manifest, files = bundle
    deck_path = "content/decks/测试 池/构筑.YDK"
    if failure in {"invalid_ydk", "oversize"}:
        files[deck_path] = b"not-a-deck" if failure == "invalid_ydk" else random.Random(5).randbytes(512 * 1024 + 1)
        row = manifest["optional_content"]["files"][0]
        row.update(size=len(files[deck_path]), sha256=hashlib.sha256(files[deck_path]).hexdigest())
    root = tmp_path / "link"
    imported = LinkModelRepository(root).import_gkg(write_bundle(tmp_path, manifest, files), "test.gkg")
    archive_id = imported["content"]["archive_id"]
    if failure == "tampered":
        (root / imported["content"]["archive_path"] / deck_path).write_bytes(b"changed")
    if failure == "wrong_origin":
        source_manifest = root / imported["content"]["archive_path"] / "contents.json"
        metadata = json.loads(source_manifest.read_text(encoding="utf-8"))
        metadata["package_name"] = "other"
        source_manifest.write_text(json.dumps(metadata), encoding="utf-8")
    if failure in {"invalid_ydk", "oversize"}:
        result = install_package_decks(root, archive_id)
        assert len(result["skipped"]) == 1 and not result["installed"]
    else:
        with pytest.raises(ValueError):
            install_package_decks(root, "../outside" if failure == "traversal" else archive_id)
    assert LinkDeckRepository(root).discover() == []


# 真实 HTTP 接口按鉴权确认入库并供统一配置选用，不自动更换对战卡组
def test_package_decks_authenticated_api_and_selection(tmp_path, bundle):
    from aiohttp.test_utils import TestClient, TestServer
    from app_config import ServiceConfig
    from service.http_api import create_http_app
    from service.session_manager import LinkSessionManager
    manifest, files = bundle
    root = tmp_path / "link"
    imported = LinkModelRepository(root).import_gkg(write_bundle(tmp_path, manifest, files), "test.gkg")
    (root / "config.yaml").write_text("agent:\n  deck: original\n", encoding="utf-8")

    # 在真实异步服务中验证入库后的查询选择与删除保护
    async def exercise():
        manager = LinkSessionManager(root / "config.yaml", runtime_builder=lambda _: None)
        app = create_http_app(manager, ServiceConfig(api_token="test-gkg-token"))
        headers = {"Authorization": "Bearer test-gkg-token"}
        async with TestClient(TestServer(app)) as client:
            payload = {"archive_id": imported["content"]["archive_id"]}
            unauthenticated = await client.post("/api/v1/models/import-decks", json=payload)
            assert unauthenticated.status == 401
            bad = await client.post("/api/v1/models/import-decks", headers=headers, json={"archive_id": "../outside"})
            assert bad.status == 400
            response = await client.post("/api/v1/models/import-decks", headers=headers, json=payload)
            result = await response.json()
            assert response.status == 201, result
            reference = result["data"]["installed"][0]
            configuration = await client.get("/api/v1/configuration", headers=headers)
            snapshot = (await configuration.json())["data"]
            assert snapshot["agent"]["deck"] == "original"
            assert snapshot["deck_catalog"][0]["deck_ref"] == reference
            selected = await client.patch("/api/v1/configuration", headers=headers,
                json={"expected_revision": snapshot["revision"], "patch": {"agent": {"deck": reference}}})
            assert selected.status == 200, await selected.text()
            deleted = await client.delete("/api/v1/decks/local", headers=headers, json={"deck_ref": reference})
            assert deleted.status == 409
    asyncio.run(exercise())


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
