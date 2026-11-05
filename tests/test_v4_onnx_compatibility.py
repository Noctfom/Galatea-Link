# 冻结 ONNX 兼容回归，实际执行旧图并验证新增卡片降为未知及结构拒绝边界

import hashlib
import json
import shutil
import zipfile

import numpy as np
import pytest

from model_protocols import load_model_backend
from model_protocols.v4.encoder import GalateaEncoder
from service.model_repository import LinkModelRepository
from tests.v4_fixtures import write_v4_assets, v4_snapshot


# 构造具有完整 V4 输入签名且冻结旧语义数值的轻量 ONNX 图
def frozen_graph(root, assets):
    onnx = pytest.importorskip("onnx")
    arrays = GalateaEncoder(assets).encode(v4_snapshot(), 0)
    helper = onnx.helper
    inputs = [helper.make_tensor_value_info(name, helper.np_dtype_to_tensor_dtype(array.dtype), array.shape)
        for name, array in arrays.items()]
    table = onnx.numpy_helper.from_array(assets.lookup.number[:, 0, 0].astype(np.float32), "frozen_semantics")
    nodes = [helper.make_node("Gather", ["frozen_semantics", "card_idx"], ["action_logits"]),
        helper.make_node("ReduceSum", ["action_logits"], ["values"], axes=[1], keepdims=1)]
    outputs = [helper.make_tensor_value_info("action_logits", onnx.TensorProto.FLOAT, [1, 120]),
        helper.make_tensor_value_info("values", onnx.TensorProto.FLOAT, [1, 1])]
    graph = helper.make_model(helper.make_graph(nodes, "frozen-test", inputs, outputs, [table]),
        opset_imports=[helper.make_opsetid("", 11)])
    graph.ir_version = 9
    metadata = {**assets.identity, "model_id": "00000000-0000-0000-0000-000000000404",
        "model_prefix": "galatea", "iteration": 1, "checkpoint_format_version": 3}
    helper.set_model_props(graph, {f"galatea.{key}": str(value) for key, value in metadata.items()})
    path = root / "galatea_iter_1.onnx"
    onnx.save(graph, path)
    record = {**metadata, "status": "complete", "format": "onnx", "primary": path.name,
        "files": [path.name], "external_data": []}
    manifest = {**metadata, "artifact_manifest_version": 3, "onnx": record}
    path.with_suffix(".artifacts.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path, record


# 不要求语义兼容证据即可加载旧图，保持文件不变并真实执行冻结语义
def test_stale_onnx_loads_and_preserves_frozen_semantics(tmp_path):
    old = write_v4_assets(tmp_path / "old", semantic_number=0.5)
    new = write_v4_assets(tmp_path / "new", semantic_number=1.0,
        cards={14558127: 10, 89631139: 11, 46986414: 12})
    path, _ = frozen_graph(tmp_path, old)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    backend = load_model_backend(path, asset_path=new.root)
    report = backend.metadata["asset_compatibility"]
    assert report["warning"] and report["added_card_count"] == 1
    assert report["semantic_content_changed"]
    assert "semantic_update" not in backend.metadata
    snapshot = v4_snapshot()
    logits, _, _ = backend.inference_runtime.infer(backend.encoder.encode(snapshot, 0))
    assert logits[0, 0] == 0.5
    snapshot.entities[0].code = 46986414
    assert backend.encoder.encode(snapshot, 0)["card_idx"][0, 0] == new.vocabulary.unknown_id
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    assert backend.metadata["online_inference_ready"] is True


# ONNX 仅词表追加也会给出知识滞后警告，编号错位仍不能混用
def test_append_only_warns_and_renumbering_is_rejected(tmp_path):
    old = write_v4_assets(tmp_path / "old")
    new = write_v4_assets(tmp_path / "new", cards={14558127: 10, 89631139: 11, 46986414: 12})
    assert new.onnx_compatibility(old.identity)["warning"]
    bad = write_v4_assets(tmp_path / "bad", cards={14558127: 11, 89631139: 10})
    with pytest.raises(ValueError, match="card_vocab_hash"):
        bad.validate(old.identity, frozen_onnx=True)
    with pytest.raises(ValueError):
        new.validate({**old.identity, "protocol_schema_revision": 13}, frozen_onnx=True)


# 新资产和旧图可以一起导入，随后再次覆盖同池资产仍保留警告
def test_gkg_import_accepts_old_graph_and_new_assets_without_evidence(tmp_path):
    old = write_v4_assets(tmp_path / "old", categories={"<PAD>": 0, "<UNK>": 1, "a": 2, "b": 3})
    new = write_v4_assets(tmp_path / "new", semantic_number=1.0,
        categories={"<PAD>": 0, "<UNK>": 1, "a": 3, "b": 2})
    path, record = frozen_graph(tmp_path, old)
    manifest = {**new.identity, "package_format_version": 4, "package_name": "frozen-onnx",
        "model_artifacts": [record], "models_included": [path.name],
        "model_files_included": [path.name, path.with_suffix(".artifacts.json").name],
        "includes_kb": False, "includes_staples": False, "includes_hash_mapping": False,
        "includes_code_semantics": False, "includes_static_semantics": True, "includes_card_vocab": True}
    package = tmp_path / "stale.gkg"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for source in [path, path.with_suffix(".artifacts.json"), *new.root.iterdir()]:
            archive.write(source, source.name)
    repository = LinkModelRepository(tmp_path / "link")
    target = tmp_path / "link/model_assets/v4" / record["model_id"]
    shutil.copytree(old.root, target)
    registry = target / "semantic_compatibility_v1.json"
    registry.write_text(json.dumps({"format_version": 1, "bases": {},
        "category_to_index": old.lookup.category_to_index, "requirement_to_index": {}}), encoding="utf-8")
    for _ in range(2):
        result = repository.import_gkg(package, package.name)
        assert result["warnings"] and result["warnings"][0]["warning"]
        assert not registry.exists()
    backend = load_model_backend(repository.model_root / path.name, asset_path=tmp_path / "link" / result["assets_path"])
    assert backend.metadata["asset_compatibility"]["warning"]
