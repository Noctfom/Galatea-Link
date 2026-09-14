# Docker 部署测试，验证持久目录初始化与公开配置模板

import tempfile
import unittest
from pathlib import Path

from app_config import load_app_config
from scripts.docker_entrypoint import prepare_runtime_data


class DockerDeploymentTests(unittest.TestCase):
    # 验证首次初始化会创建运行目录并复制公开配置
    def test_prepare_runtime_data_creates_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = prepare_runtime_data(root)

            self.assertEqual(config_path, root / "config.yaml")
            self.assertTrue(config_path.is_file())
            self.assertTrue((root / "decks").is_dir())
            self.assertTrue((root / "models").is_dir())
            self.assertTrue((root / "model_assets/v3").is_dir())
            self.assertTrue((root / "deploy_packages").is_dir())

    # 验证容器重启不会覆盖用户已经保存的配置
    def test_prepare_runtime_data_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.mkdir(parents=True, exist_ok=True)
            config_path = root / "config.yaml"
            config_path.write_text("custom: true\n", encoding="utf-8")

            prepared_path = prepare_runtime_data(root)

            self.assertEqual(prepared_path, config_path)
            self.assertEqual(
                config_path.read_text(encoding="utf-8"),
                "custom: true\n",
            )

    # 验证 Docker 默认模板使用统一端口与 ONNX 后端
    @unittest.skipUnless(Path("config.docker.yaml").is_file(), "需要 Docker 配置")
    def test_docker_config_uses_public_service_and_onnx(self):
        config = load_app_config("config.docker.yaml")

        self.assertEqual(config.service.host, "0.0.0.0")
        self.assertEqual(config.service.port, 8765)
        self.assertEqual(config.model.protocol, "v3")
        self.assertEqual(config.model.inference_backend, "onnxruntime")


if __name__ == "__main__":
    unittest.main()
