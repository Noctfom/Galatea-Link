# 模型推理运行时抽象，统一 PyTorch 与 ONNX Runtime 的动作分数输出

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


ONNX_NUMPY_DTYPES = {
    "tensor(float)": np.float32,
    "tensor(float16)": np.float16,
    "tensor(int64)": np.int64,
    "tensor(int32)": np.int32,
    "tensor(int16)": np.int16,
    "tensor(int8)": np.int8,
    "tensor(uint8)": np.uint8,
    "tensor(bool)": np.bool_,
}


class PyTorchInferenceRuntime:
    # 初始化已有 PyTorch 网络的推理包装器
    def __init__(self, net: Any, device: str) -> None:
        self.net = net
        self.device = device
        self.runtime_id = "pytorch"

    # 执行 PyTorch 前向并转换为后端无关数组
    def infer(
        self,
        observation: Mapping[str, Any],
    ) -> tuple[np.ndarray, np.ndarray, Any]:
        import torch

        self.net.eval()
        with torch.no_grad():
            device_inputs = {
                key: (value if hasattr(value, "to") else torch.from_numpy(np.ascontiguousarray(value))).to(self.device)
                for key, value in observation.items()
            }
            logits, values, value_input = self.net(device_inputs)
        return (
            logits.detach().cpu().numpy(),
            values.detach().cpu().numpy(),
            value_input,
        )

    # 在同一次前向中读取 V4 终局头，旧网络继续返回其实际价值输出
    def infer_with_evaluation(self, observation):
        import torch
        if not callable(getattr(self.net, 'evaluate_state', None)):
            return (*self.infer(observation), None)
        with torch.no_grad():
            inputs = {key: (value if hasattr(value, 'to') else torch.from_numpy(np.ascontiguousarray(value))).to(self.device)
                for key, value in observation.items()}
            logits, values, state, predictions = self.net(inputs, return_evaluation=True)
        return logits.detach().cpu().numpy(), values.detach().cpu().numpy(), state, {
            key: value.detach().cpu().numpy() for key, value in predictions.items()}

    # 只运行状态主干和终局头，不要求候选也不进行动作采样
    def evaluate_state(self, observation):
        import torch
        with torch.no_grad():
            inputs = {key: (value if hasattr(value, 'to') else torch.from_numpy(np.ascontiguousarray(value))).to(self.device)
                for key, value in observation.items()}
            values, _, predictions = self.net.evaluate_state(inputs)
        return values.detach().cpu().numpy(), {key: value.detach().cpu().numpy() for key, value in predictions.items()}


class OnnxInferenceRuntime:
    # 初始化受控执行提供器和固定输入签名的 ONNX 会话
    def __init__(
        self,
        model_path: str | Path,
        *,
        providers: Sequence[str] = ("CPUExecutionProvider",),
        intra_op_threads: int = 0,
    ) -> None:
        try:
            import onnxruntime as ort
        except ImportError as error:
            raise RuntimeError(
                "缺少 onnxruntime 依赖，无法加载 ONNX 模型"
            ) from error

        normalized_providers = tuple(str(item).strip() for item in providers)
        if not normalized_providers or any(not item for item in normalized_providers):
            raise ValueError("ONNX Execution Provider 列表不能为空")
        options = ort.SessionOptions()
        if intra_op_threads > 0:
            options.intra_op_num_threads = int(intra_op_threads)
        self.session = ort.InferenceSession(
            str(Path(model_path).resolve()),
            sess_options=options,
            providers=list(normalized_providers),
        )
        self.runtime_id = "onnxruntime"
        session_inputs = self.session.get_inputs()
        self.input_names = tuple(item.name for item in session_inputs)
        self.input_dtypes = {
            item.name: ONNX_NUMPY_DTYPES.get(item.type)
            for item in session_inputs
        }
        self.output_names = tuple(item.name for item in self.session.get_outputs())
        self.metadata = dict(self.session.get_modelmeta().custom_metadata_map)

    # 将 NumPy 或 PyTorch 输入转换为连续 NumPy 数组
    @staticmethod
    def _to_numpy(value: Any) -> np.ndarray:
        if isinstance(value, np.ndarray):
            return np.ascontiguousarray(value)
        detach = getattr(value, "detach", None)
        if callable(detach):
            value = detach()
        cpu = getattr(value, "cpu", None)
        if callable(cpu):
            value = cpu()
        numpy_method = getattr(value, "numpy", None)
        if callable(numpy_method):
            value = numpy_method()
        return np.ascontiguousarray(np.asarray(value))

    # 校验输入完整性并执行 ONNX 前向推理
    def infer(
        self,
        observation: Mapping[str, Any],
        *, evaluation=False,
    ):
        missing = [name for name in self.input_names if name not in observation]
        unexpected = sorted(set(observation).difference(self.input_names))
        if missing or unexpected:
            raise ValueError(
                "ONNX 输入签名不匹配 "
                f"missing={missing} unexpected={unexpected}"
            )
        feed = {}
        for name in self.input_names:
            value = self._to_numpy(observation[name])
            expected_dtype = self.input_dtypes[name]
            if expected_dtype is None:
                raise ValueError(f"不支持的 ONNX 输入类型: {name}")
            feed[name] = np.ascontiguousarray(
                value.astype(expected_dtype, copy=False)
            )
        output_names = ["action_logits", "values"]
        if not set(output_names).issubset(self.output_names):
            raise ValueError(
                "ONNX 输出签名不匹配 "
                f"expected={output_names} actual={list(self.output_names)}"
            )
        extra_names = ['terminal_outcome_logits', 'terminal_remaining']
        if evaluation and set(extra_names).issubset(self.output_names):
            output_names += extra_names
        outputs = self.session.run(output_names, feed)
        result = np.asarray(outputs[0]), np.asarray(outputs[1]), None
        if evaluation:
            predictions = dict(zip(extra_names, outputs[2:])) if len(outputs) == 4 else None
            return (*result, predictions)
        return result

    # 使用图内已导出的终局头，旧双输出图保持价值可用但不虚构胜率
    def infer_with_evaluation(self, observation):
        return self.infer(observation, evaluation=True)

    # 无候选场面只读取价值和已导出预测，忽略动作输出
    def evaluate_state(self, observation):
        _, values, _, predictions = self.infer_with_evaluation(observation)
        return values, predictions
