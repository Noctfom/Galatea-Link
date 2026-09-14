# 部署入口测试，验证跨平台快速启动脚本与文档保持一致

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DeploymentScriptTests(unittest.TestCase):
    # 验证根目录提供 Windows 与 Linux 本地启动入口
    def test_local_start_scripts_exist_with_onnx_default(self):
        windows_script = PROJECT_ROOT / "start_windows.ps1"
        linux_script = PROJECT_ROOT / "start_linux.sh"

        self.assertTrue(windows_script.is_file())
        self.assertTrue(linux_script.is_file())
        self.assertIn(
            "requirements-onnx.txt",
            windows_script.read_text(encoding="utf-8-sig"),
        )
        self.assertIn(
            "requirements-onnx.txt",
            linux_script.read_text(encoding="utf-8"),
        )

    # 验证 Windows 脚本带有旧版 PowerShell 所需的 UTF-8 BOM
    def test_windows_scripts_use_utf8_bom(self):
        for relative_path in (
            "start_windows.ps1",
            "scripts/deploy_docker.ps1",
            "scripts/install_docker.ps1",
        ):
            with self.subTest(path=relative_path):
                self.assertTrue(
                    (PROJECT_ROOT / relative_path).read_bytes().startswith(
                        b"\xef\xbb\xbf"
                    )
                )

    # 验证依赖文件可被中文 Windows 自带 pip 按 UTF-8 读取
    def test_requirement_files_declare_utf8(self):
        for requirement_path in PROJECT_ROOT.glob("requirements*.txt"):
            with self.subTest(path=requirement_path.name):
                first_line = requirement_path.read_text(
                    encoding="utf-8"
                ).splitlines()[0]
                self.assertEqual(first_line, "# -*- coding: utf-8 -*-")

    # 验证快速部署文档同时提供在线 Docker 与本地启动命令
    def test_deployment_document_lists_quick_commands(self):
        document = (PROJECT_ROOT / "docs/DOCKER_DEPLOYMENT.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("scripts/install_docker.ps1 | iex", document)
        self.assertIn("scripts/install_docker.sh | sh", document)
        self.assertIn("start_windows.ps1", document)
        self.assertIn("start_linux.sh", document)


if __name__ == "__main__":
    unittest.main()
