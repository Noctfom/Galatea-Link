# 模型协议注册表测试，覆盖旧版识别、V3 识别和未知版本拒绝

import tempfile
import unittest
from pathlib import Path

import torch

from model_protocols import inspect_model_checkpoint, load_model_backend


class ModelProtocolRegistryTests(unittest.TestCase):
    # 写入仅供元数据探测使用的安全测试检查点
    def _write_checkpoint(self, directory, payload):
        path = Path(directory) / "galatea_iter_1.pth"
        torch.save(payload, path)
        return path

    # 验证格式一检查点继续映射到旧模型协议
    def test_legacy_checkpoint_is_inferred_as_protocol_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_checkpoint(directory, {
                "checkpoint_format_version": 1,
                "model_id": "00000000-0000-0000-0000-000000000001",
                "model_prefix": "galatea",
                "run_id": "legacy",
                "iteration": 1,
                "net_config": {},
                "model_state_dict": {"weight": torch.zeros(1)},
            })
            metadata = inspect_model_checkpoint(path)
        self.assertEqual(metadata["model_protocol_version"], 1)

    # 验证格式二检查点必须显式声明 V3 网络协议
    def test_v3_checkpoint_is_recognized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_checkpoint(directory, {
                "checkpoint_format_version": 2,
                "model_protocol_version": 3,
                "model_id": "00000000-0000-0000-0000-000000000003",
                "model_prefix": "galatea",
                "run_id": "v3",
                "iteration": 1,
                "net_config": {"model_protocol_version": 3},
                "model_state_dict": {"weight": torch.zeros(1)},
            })
            metadata = inspect_model_checkpoint(path)
        self.assertEqual(metadata["checkpoint_format_version"], 2)
        self.assertEqual(metadata["model_protocol_version"], 3)

    # 验证未知模型协议不会误入任意已注册网络
    def test_unknown_checkpoint_protocol_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_checkpoint(directory, {
                "checkpoint_format_version": 3,
                "model_protocol_version": 4,
                "net_config": {"model_protocol_version": 4},
            })
            with self.assertRaisesRegex(ValueError, "不支持"):
                inspect_model_checkpoint(path)

    # 验证显式固定协议不能覆盖检查点的真实身份
    def test_protocol_pin_mismatch_is_rejected_before_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_checkpoint(directory, {
                "checkpoint_format_version": 2,
                "model_protocol_version": 3,
                "model_id": "00000000-0000-0000-0000-000000000003",
                "model_prefix": "galatea",
                "run_id": "v3",
                "iteration": 1,
                "net_config": {"model_protocol_version": 3},
                "model_state_dict": {"weight": torch.zeros(1)},
            })
            with self.assertRaisesRegex(ValueError, "不一致"):
                load_model_backend(path, protocol="v1")


if __name__ == "__main__":
    unittest.main()
