# V4 资产与运行时回归，覆盖真实前向协议拒绝隔离以及 NumPy 无 Torch 路径

import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import zipfile

import numpy as np
import pytest

from model_protocols import inspect_model_checkpoint, load_model_backend
from model_protocols.v4.assets import load_v4_assets, inspect_protocol_identity
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.runtime_context import use_assets
from service.model_repository import LinkModelRepository
from tests.v4_fixtures import write_v4_assets, v4_snapshot, write_v4_checkpoint


# 在独立临时目录建立完整可校验的资产
@pytest.fixture
def assets(tmp_path):
    return write_v4_assets(tmp_path / "assets")


# 确认词表语义被篡改时拒绝加载
def test_v4_assets_reject_tampering(assets):
    catalog = assets.root / "semantic_lookup_v1.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    payload["semantic_lookup_hash"] = "0" * 64
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="logical hash"):
        load_v4_assets(assets.root)


# 在创建网络前拒绝结构修订及哈希错配
def test_v4_schema_identity_is_strict(assets):
    for patch in ({"protocol_schema_revision": 13}, {"protocol_schema_hash": "0" * 64}, {"card_vocab_card_count": True}):
        with pytest.raises(ValueError):
            inspect_protocol_identity({**assets.identity, **patch})


# 编码完整快照且不将旧在线快照伪装成 V4
def test_v4_numpy_encoder_and_online_gate(assets):
    from core.gamestate import DuelState
    encoder = GalateaEncoder(assets)
    arrays = encoder.encode(v4_snapshot(), 0)
    assert arrays["card_idx"][0, 0] == 10
    assert arrays["act_mask"][0, :2].all()
    assert arrays["deck_profile_initial_count"][0, 0] == 2
    assert arrays["hint_mask"].shape == (1, 128)
    assert all(isinstance(value, np.ndarray) for value in arrays.values())
    with pytest.raises(ValueError, match="在线观测合同"):
        encoder.encode(DuelState().get_snapshot(), 0)


# 在新进程验证资产和编码路径不会导入 Torch
def test_v4_numpy_path_does_not_import_torch(assets):
    code = "from model_protocols.v4.assets import load_v4_assets; from model_protocols.v4.encoder import GalateaEncoder; from tests.v4_fixtures import v4_snapshot; import sys; a=load_v4_assets(sys.argv[1]); GalateaEncoder(a).encode(v4_snapshot(),0); assert 'torch' not in sys.modules"
    subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(assets.root)], check=True, cwd=Path(__file__).resolve().parents[1], timeout=30)


# 验证 PTH 的真实网络前向和对在线不可用状态的公开声明
def test_v4_pytorch_forward_and_metadata(assets, tmp_path):
    path, _ = write_v4_checkpoint(tmp_path, assets)
    assert inspect_model_checkpoint(path)["model_protocol_version"] == 4
    backend = load_model_backend(path, asset_path=assets.root, protocol="v4")
    logits, values, _ = backend.inference_runtime.infer(backend.encoder.encode(v4_snapshot(), 0))
    assert logits.shape == (1, 120)
    assert values.shape == (1, 1)
    assert np.isfinite(logits).all() and np.isfinite(values).all()
    assert backend.metadata["offline_inference_ready"] is True
    assert backend.metadata["online_inference_ready"] is False


# 验证 GKG4 导入及坏资产在安装模型前被拒绝
@pytest.mark.parametrize("failure", [None, "missing_compiled_assets", "record_identity"])
def test_import_v4_gkg_preserves_protocol_identity(assets, tmp_path, failure):
    path, metadata = write_v4_checkpoint(tmp_path, assets)
    record = {**assets.identity, "format": "pytorch_checkpoint", "model_id": metadata["model_id"],
        "model_prefix": "galatea", "iteration": 1, "primary": path.name, "files": [path.name], "status": "complete"}
    if failure == "record_identity":
        record["semantic_lookup_hash"] = "0" * 64
    manifest = {**assets.identity, "package_format_version": 4, "package_name": "v4-test", "models_included": [path.name],
        "model_artifacts": [record], "model_files_included": [path.name], "includes_card_vocab": True,
        "includes_static_semantics": True, "includes_kb": False, "includes_staples": False,
        "includes_hash_mapping": False, "includes_code_semantics": False}
    package = tmp_path / "test.gkg"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.write(path, path.name)
        for asset in assets.root.iterdir():
            if failure == "missing_compiled_assets" and asset.suffix == ".npz":
                continue
            archive.write(asset, asset.name)
    repository = LinkModelRepository(tmp_path / "link")
    if failure:
        with pytest.raises((ValueError, FileNotFoundError)):
            repository.import_gkg(package, package.name)
        assert not list(repository.model_root.iterdir())
        return
    imported = repository.import_gkg(package, package.name)
    assert imported["model_protocol_version"] == 4
    assert f"model_assets/v4/{metadata['model_id']}" in imported["assets_path"]
    assert repository.discover()["models"][0]["online_inference_ready"] is False
    assert not list((tmp_path / "link/model_assets/v3").iterdir())


# 自动识别的 V4 资产也不能误用 V3 的下载和重建维护器
@pytest.mark.parametrize("pin", ["auto", "v4"])
def test_v4_maintenance_rejects_legacy_builders(tmp_path, pin):
    from service.asset_manager import LinkAssetManager
    manager = LinkAssetManager(tmp_path, lambda: {"protocol": pin, "assets_path": "model_assets/v4/test-model"})
    status = manager.get_status()
    assert status["model_protocol_version"] == 4
    assert status["semantic_maintenance_supported"] is False
    for operation in (manager.sync_card_data, manager.sync_semantic_bundle, manager.rebuild_semantic_bundle):
        with pytest.raises(ValueError, match="GKG4"):
            operation()


# ONNX 外置权重必须同时出现在图引用和同轮制品清单中
@pytest.mark.parametrize("failure", [None, "undeclared", "escape", "overflow", "extra_file"])
def test_onnx_external_data_is_validated_before_runtime(assets, tmp_path, failure):
    onnx = pytest.importorskip("onnx")
    from model_protocols.v4.backend import inspect_v4_onnx
    path = tmp_path / "galatea_iter_1.onnx"
    data_name = path.name + ".data"
    (tmp_path / data_name).write_bytes(b"\0" * 4)
    tensor = onnx.TensorProto(name="weight", data_type=onnx.TensorProto.FLOAT, dims=[1], data_location=onnx.TensorProto.EXTERNAL)
    location = "../outside.onnx.data" if failure == "escape" else data_name
    tensor.external_data.add(key="location", value=location)
    tensor.external_data.add(key="offset", value="0")
    tensor.external_data.add(key="length", value="8" if failure == "overflow" else "4")
    graph = onnx.helper.make_graph([], "test", [], [], initializer=[tensor])
    path.write_bytes(onnx.helper.make_model(graph).SerializeToString())
    identity = {**assets.identity, "checkpoint_format_version": 3,
        "model_id": "00000000-0000-0000-0000-000000000404", "model_prefix": "galatea", "iteration": 1}
    files = [path.name] if failure == "undeclared" else [path.name, location]
    if failure == "extra_file":
        (tmp_path / "extra.onnx.data").write_bytes(b"unused")
        files.append("extra.onnx.data")
    manifest = {**identity, "artifact_manifest_version": 3,
        "onnx": {**identity, "status": "complete", "primary": path.name, "files": files, "external_data": [location]}}
    if failure:
        with pytest.raises((ValueError, FileNotFoundError)):
            inspect_v4_onnx(path, manifest)
    else:
        assert inspect_v4_onnx(path, manifest)["model_protocol_version"] == 4


# 在指定只读 Core 源码时逐字段比较 NumPy 与原始 Torch 编码器
@pytest.mark.skipif(not os.environ.get("GALATEA_CORE_READONLY_ROOT"), reason="需要显式指定只读 Core 源码对照")
def test_numpy_encoder_matches_readonly_core_reference(assets):
    source = (Path(os.environ["GALATEA_CORE_READONLY_ROOT"]) / "feature_encoder.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in {"card_vocab", "deck_protocol", "data_types", "event_history", "game_constants", "semantic_lookup", "public_observation", "public_hint_semantics", "card_reader"}:
            node.level = 1
    module = types.ModuleType("model_protocols.v4.core_reference_encoder")
    module.__package__ = "model_protocols.v4"
    exec(compile(ast.fix_missing_locations(tree), "<readonly Core encoder>", "exec"), module.__dict__)
    with use_assets(assets):
        reference = module.GalateaEncoder(card_vocabulary=assets.vocabulary)
    for player in (0, 1):
        snapshot = v4_snapshot(player)
        with use_assets(assets):
            expected = reference.encode(snapshot, player)
        actual = GalateaEncoder(assets).encode(snapshot, player)
        assert actual.keys() == expected.keys()
        for key, value in actual.items():
            np.testing.assert_array_equal(value, expected[key].numpy(), err_msg=key)
            assert value.dtype == expected[key].numpy().dtype, key
