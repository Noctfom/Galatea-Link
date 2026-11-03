# V4 语义更新回归，区分可验证的 PTH 内容更新和冻结的 ONNX 身份

import hashlib
import json
import shutil
import zipfile
from unittest.mock import Mock

import pytest

from model_protocols import load_model_backend
from model_protocols.v4.assets import load_v4_assets
from model_protocols.v4.semantic_compatibility import semantic_compatibility_descriptor
from service.model_repository import LinkModelRepository
from tests.v4_fixtures import write_v4_assets, write_v4_checkpoint, v4_snapshot


CONTRACT = {"encoder": {"model": "test-encoder", "revision": "test"},
    "policy": {"chunking": "test-v1"}}


# 构造仅语义内容变化且保留全部坐标系证据的两套资产
@pytest.fixture
def updated_assets(tmp_path):
    old = write_v4_assets(tmp_path / "old", code_contract=CONTRACT)
    new = write_v4_assets(tmp_path / "new", semantic_number=1.0, code_contract=CONTRACT)
    registry = {"format_version": 1, "category_to_index": new.lookup.category_to_index,
        "requirement_to_index": new.lookup.requirement_to_index,
        "bases": {item.identity["semantic_lookup_hash"]: semantic_compatibility_descriptor(item.lookup)
            for item in (old, new)}}
    (new.root / "semantic_compatibility_v1.json").write_text(json.dumps(registry), encoding="utf-8")
    return old, load_v4_assets(new.root)


# 验证可兼容更新实际使用新表且不改写旧检查点文件
def test_compatible_pytorch_load_uses_new_semantics(updated_assets, tmp_path):
    old, new = updated_assets
    path, metadata = write_v4_checkpoint(tmp_path, old)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    backend = load_model_backend(path, asset_path=new.root)
    assert backend.metadata["semantic_lookup_hash"] == old.identity["semantic_lookup_hash"]
    assert backend.metadata["semantic_update"]["status"] == "compatible_content_update"
    assert backend.metadata["runtime_asset_identity"] == new.identity
    assert float(backend.net.semantic_number_table[10, 0, 0]) == 1.0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    logits, value, _ = backend.inference_runtime.infer(backend.encoder.encode(v4_snapshot(), 0))
    assert logits.shape == (1, 120) and value.shape == (1, 1)


# 冻结制品的校验仍拒绝静默更换内容且缺少证据不能放宽
def test_strict_assets_and_missing_evidence_are_rejected(updated_assets):
    old, new = updated_assets
    with pytest.raises(ValueError, match="semantic_lookup_hash"):
        new.validate(old.identity)
    (new.root / "semantic_compatibility_v1.json").unlink()
    with pytest.raises(ValueError, match="compatibility evidence"):
        new.validate(old.identity, allow_semantic_updates=True)


# 编码器和标签编号证据不一致不能冒充普通知识更新
@pytest.mark.parametrize("field", ["encoder", "policy", "category_prefix_hash", "card_vocab_hash", "code_dimension"])
def test_incompatible_semantic_coordinates_are_rejected(updated_assets, field):
    old, new = updated_assets
    descriptor = dict(semantic_compatibility_descriptor(old.lookup))
    descriptor[field] = "invalid"
    with pytest.raises(ValueError):
        new.validate({**old.identity, "semantic_compatibility": descriptor}, allow_semantic_updates=True)


# 构造带兼容清单的新 GKG4，检查点仍保留原始语义身份
def write_updated_package(tmp_path, old, new):
    path, metadata = write_v4_checkpoint(tmp_path, old)
    record = {**old.identity, "format": "pytorch_checkpoint", "model_id": metadata["model_id"],
        "model_prefix": metadata["model_prefix"], "iteration": 1, "primary": path.name,
        "files": [path.name], "status": "complete"}
    manifest = {**new.identity, "package_format_version": 4, "package_name": "updated",
        "models_included": [path.name], "model_files_included": [path.name], "model_artifacts": [record],
        "includes_kb": False, "includes_staples": False, "includes_hash_mapping": False,
        "includes_code_semantics": False, "includes_static_semantics": True, "includes_card_vocab": True}
    package = tmp_path / "updated.gkg"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.write(path, path.name)
        for asset in new.root.iterdir():
            archive.write(asset, asset.name)
    return package, metadata


# 新版部署包正常导入且不会丢失后续加载需要的兼容证据
def test_new_gkg_import_preserves_compatibility_registry(updated_assets, tmp_path):
    old, new = updated_assets
    package, metadata = write_updated_package(tmp_path, old, new)
    repository = LinkModelRepository(tmp_path / "link")
    result = repository.import_gkg(package, package.name)
    root = tmp_path / "link" / result["assets_path"]
    assert (root / "semantic_compatibility_v1.json").is_file()
    backend = load_model_backend(repository.model_root / "galatea_iter_1.pth", asset_path=root)
    assert backend.metadata["semantic_update"]["status"] == "compatible_content_update"


# 同池冻结 ONNX 不阻止资产更新，导入结果明确指出图内知识仍旧
def test_existing_frozen_onnx_warns_on_pool_asset_replacement(updated_assets, tmp_path):
    old, new = updated_assets
    package, metadata = write_updated_package(tmp_path, old, new)
    repository = LinkModelRepository(tmp_path / "link")
    repository.discover = Mock(return_value={"models": [{**old.identity, "model_id": metadata["model_id"], "format": "onnx"}]})
    result = repository.import_gkg(package, package.name)
    assert result["warnings"][0]["status"] == "frozen_onnx_outdated"
    assert result["warnings"][0]["semantic_content_changed"] is True
    assert (repository.model_root / "galatea_iter_1.pth").is_file()


# 更新时合并既有历史证据而不改写原始部署包
def test_pool_update_preserves_older_compatibility_evidence(updated_assets, tmp_path):
    old, new = updated_assets
    package, metadata = write_updated_package(tmp_path, old, new)
    original_package_hash = hashlib.sha256(package.read_bytes()).hexdigest()
    repository = LinkModelRepository(tmp_path / "link")
    target = tmp_path / "link/model_assets/v4" / metadata["model_id"]
    shutil.copytree(old.root, target)
    ancestor = write_v4_assets(tmp_path / "ancestor", semantic_number=0.5, code_contract=CONTRACT)
    registry = {"format_version": 1, "category_to_index": old.lookup.category_to_index,
        "requirement_to_index": old.lookup.requirement_to_index,
        "bases": {item.identity["semantic_lookup_hash"]: semantic_compatibility_descriptor(item.lookup)
            for item in (old, ancestor)}}
    (target / "semantic_compatibility_v1.json").write_text(json.dumps(registry), encoding="utf-8")
    repository.import_gkg(package, package.name)
    updated = json.loads((target / "semantic_compatibility_v1.json").read_text(encoding="utf-8"))
    assert set(registry["bases"]).issubset(updated["bases"])
    assert new.identity["semantic_lookup_hash"] in updated["bases"]
    assert hashlib.sha256(package.read_bytes()).hexdigest() == original_package_hash
