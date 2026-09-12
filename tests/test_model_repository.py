# Link 模型仓库测试，验证模型协议 V3 GKG 安全导入和资产隔离

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from service.model_repository import LinkModelRepository


MODEL_ID = "00000000-0000-0000-0000-000000000303"


class LinkModelRepositoryTests(unittest.TestCase):
    # 创建带最小完整语义资产的模型协议 V3 GKG 包
    def _write_valid_gkg(self, root: Path) -> Path:
        stage = root / "stage"
        stage.mkdir()
        graph_name = "galatea_iter_3.onnx"
        manifest_name = "galatea_iter_3.artifacts.json"
        (stage / graph_name).write_bytes(b"test-onnx")
        artifact_manifest = {
            "artifact_manifest_version": 2,
            "checkpoint_format_version": 2,
            "model_protocol_version": 3,
            "model_id": MODEL_ID,
            "model_prefix": "galatea",
            "iteration": 3,
            "run_id": "test-run",
            "onnx": {
                "format": "onnx",
                "model_id": MODEL_ID,
                "model_prefix": "galatea",
                "iteration": 3,
                "model_protocol_version": 3,
                "primary": graph_name,
                "files": [graph_name],
                "external_data": [],
                "status": "complete",
            },
        }
        (stage / manifest_name).write_text(
            json.dumps(artifact_manifest),
            encoding="utf-8",
        )
        (stage / "knowledge_base.json").write_text("{}", encoding="utf-8")
        (stage / "code_embeddings_idx.json").write_text("{}", encoding="utf-8")
        np.save(stage / "code_embeddings.npy", np.empty((0, 32), dtype=np.float32))
        connection = sqlite3.connect(stage / "cards.cdb")
        connection.execute("CREATE TABLE datas (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE texts (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO datas (id) VALUES (14558127)")
        connection.execute("INSERT INTO texts (id, name) VALUES (14558127, '灰流丽')")
        connection.commit()
        connection.close()
        (stage / "meta_staples.json").write_text(
            "[14558127]",
            encoding="utf-8",
        )
        model_record = {
            "format": "onnx",
            "model_id": MODEL_ID,
            "model_prefix": "galatea",
            "iteration": 3,
            "model_protocol_version": 3,
            "primary": graph_name,
            "files": [graph_name],
            "status": "complete",
        }
        package_manifest = {
            "package_format_version": 2,
            "package_name": "galatea-v3-test",
            "models_included": [graph_name],
            "model_artifacts": [model_record],
            "model_files_included": [graph_name, manifest_name],
            "includes_kb": True,
            "includes_staples": True,
            "includes_hash_mapping": False,
            "includes_code_semantics": True,
        }
        (stage / "manifest.json").write_text(
            json.dumps(package_manifest),
            encoding="utf-8",
        )
        package_path = root / "galatea-v3-test.gkg"
        with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.iterdir()):
                archive.write(path, arcname=path.name)
        return package_path

    # 创建 Core 可生成的不含模型纯运行资产 GKG 包
    def _write_asset_only_gkg(self, root: Path) -> Path:
        stage = root / "asset-stage"
        stage.mkdir()
        (stage / "knowledge_base.json").write_text("{}", encoding="utf-8")
        (stage / "code_embeddings_idx.json").write_text("{}", encoding="utf-8")
        np.save(stage / "code_embeddings.npy", np.empty((0, 32), dtype=np.float32))
        (stage / "meta_staples.json").write_text("[14558127]", encoding="utf-8")
        manifest = {
            "package_format_version": 2,
            "package_name": "galatea-v3-assets",
            "models_included": [],
            "model_artifacts": [],
            "model_files_included": [],
            "includes_kb": True,
            "includes_staples": True,
            "includes_hash_mapping": False,
            "includes_code_semantics": True,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        package_path = root / "galatea-v3-assets.gkg"
        with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.iterdir()):
                archive.write(path, arcname=path.name)
        return package_path

    # 验证有效 GKG 被安装并将语义资产隔离到模型 UUID 目录
    def test_imports_v3_gkg_and_discovers_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = self._write_valid_gkg(root)
            repository = LinkModelRepository(root / "link")

            result = repository.import_gkg(package_path, package_path.name)
            catalog = repository.discover("./models/galatea_iter_3.onnx")

            self.assertEqual(result["model_id"], MODEL_ID)
            self.assertEqual(len(catalog["models"]), 1)
            model = catalog["models"][0]
            self.assertTrue(model["selected"])
            self.assertTrue(model["assets_available"])
            self.assertEqual(
                model["assets_path"],
                f"./model_assets/v3/{MODEL_ID}",
            )
            assets = root / "link" / "model_assets" / "v3" / MODEL_ID
            self.assertTrue((assets / "cards.cdb").is_file())
            self.assertEqual(
                json.loads((assets / "meta_staples.json").read_text(encoding="utf-8")),
                [14558127],
            )

    # 验证纯资产 GKG 能绑定当前模型池而不伪造包内模型身份
    def test_imports_asset_only_gkg_for_selected_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = self._write_asset_only_gkg(root)
            repository = LinkModelRepository(root / "link")

            result = repository.import_gkg(
                package_path,
                package_path.name,
                MODEL_ID,
            )

            self.assertIsNone(result["model_id"])
            self.assertEqual(result["asset_target_model_id"], MODEL_ID)
            self.assertEqual(
                result["assets_path"],
                f"./model_assets/v3/{MODEL_ID}",
            )
            assets = root / "link" / "model_assets" / "v3" / MODEL_ID
            self.assertEqual(
                json.loads((assets / "meta_staples.json").read_text(encoding="utf-8")),
                [14558127],
            )

    # 验证 GKG 解包拒绝目录穿越成员
    def test_rejects_gkg_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = root / "unsafe.gkg"
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr("../manifest.json", "{}")
            repository = LinkModelRepository(root / "link")

            with self.assertRaisesRegex(ValueError, "不安全路径"):
                repository.import_gkg(package_path, package_path.name)


if __name__ == "__main__":
    unittest.main()
