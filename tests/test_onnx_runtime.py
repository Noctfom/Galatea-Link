# V3 ONNX 推理测试，验证制品清单、输入类型和无 Torch 编码路径

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from model_protocols import inspect_onnx_artifact
from model_protocols.inference_runtime import OnnxInferenceRuntime


class FakeOnnxSession:
    # 初始化并记录最近一次 ONNX 输入
    def __init__(self) -> None:
        self.feed = None

    # 返回固定输出并保存输入数据类型
    def run(self, output_names, feed):
        self.feed = feed
        return (
            np.asarray([[3.0, 1.0]], dtype=np.float32),
            np.asarray([[0.25]], dtype=np.float32),
        )


class OnnxArtifactTests(unittest.TestCase):
    # 创建最小但完整的 V3 ONNX 测试制品组
    def _write_artifact(self, directory: str, *, include_data: bool = True) -> Path:
        root = Path(directory)
        graph_path = root / "galatea_iter_1.onnx"
        graph_path.write_bytes(b"onnx")
        if include_data:
            (root / "galatea_iter_1.onnx.data").write_bytes(b"weights")
        manifest = {
            "artifact_manifest_version": 2,
            "checkpoint_format_version": 2,
            "model_protocol_version": 3,
            "model_id": "00000000-0000-0000-0000-000000000003",
            "model_prefix": "galatea",
            "iteration": 1,
            "onnx": {
                "format": "onnx",
                "model_id": "00000000-0000-0000-0000-000000000003",
                "model_prefix": "galatea",
                "iteration": 1,
                "model_protocol_version": 3,
                "primary": graph_path.name,
                "files": [graph_path.name, "galatea_iter_1.onnx.data"],
                "external_data": ["galatea_iter_1.onnx.data"],
                "status": "complete",
            },
        }
        graph_path.with_suffix(".artifacts.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        return graph_path

    # 验证完整制品清单可以识别为模型协议 V3
    def test_inspects_complete_v3_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            graph_path = self._write_artifact(directory)
            metadata = inspect_onnx_artifact(graph_path)

        self.assertEqual(metadata["model_protocol_version"], 3)
        self.assertEqual(metadata["iteration"], 1)

    # 验证缺失外置权重时在创建推理会话前失败
    def test_rejects_missing_external_data(self):
        with tempfile.TemporaryDirectory() as directory:
            graph_path = self._write_artifact(directory, include_data=False)
            with self.assertRaisesRegex(FileNotFoundError, "制品文件不存在"):
                inspect_onnx_artifact(graph_path)


class OnnxInferenceRuntimeTests(unittest.TestCase):
    # 验证运行时按 ONNX 图签名转换输入数据类型
    def test_casts_inputs_to_graph_dtypes(self):
        runtime = OnnxInferenceRuntime.__new__(OnnxInferenceRuntime)
        runtime.session = FakeOnnxSession()
        runtime.input_names = ("index", "mask")
        runtime.input_dtypes = {
            "index": np.int64,
            "mask": np.bool_,
        }
        runtime.output_names = ("action_logits", "values")

        logits, values, value_input = runtime.infer(
            {
                "index": np.asarray([[1]], dtype=np.int32),
                "mask": np.asarray([[1]], dtype=np.uint8),
            }
        )

        self.assertEqual(runtime.session.feed["index"].dtype, np.int64)
        self.assertEqual(runtime.session.feed["mask"].dtype, np.bool_)
        self.assertEqual(logits.shape, (1, 2))
        self.assertEqual(values.shape, (1, 1))
        self.assertIsNone(value_input)

    # 验证输入缺失和意外字段都会被拒绝
    def test_rejects_input_signature_mismatch(self):
        runtime = OnnxInferenceRuntime.__new__(OnnxInferenceRuntime)
        runtime.session = FakeOnnxSession()
        runtime.input_names = ("required",)
        runtime.input_dtypes = {"required": np.float32}
        runtime.output_names = ("action_logits", "values")

        with self.assertRaisesRegex(ValueError, "missing"):
            runtime.infer({"unexpected": np.zeros(1)})


if __name__ == "__main__":
    unittest.main()
