# V4 编码器运行包装，支持原生离线输入和明确标注历史缺失的在线兼容输入

import numpy as np

from .data_types import GameSnapshot
from .feature_encoder import CoreV4NumpyEncoder
from .runtime_context import use_assets


class GalateaEncoder:
    # 为单个模型绑定独立词表语义和静态卡组缓存
    def __init__(self, assets, *, frozen_card_count=None):
        self.assets = assets
        self.last_online_report = None
        with use_assets(assets):
            vocabulary = assets.vocabulary if frozen_card_count is None else assets.vocabulary.prefix(frozen_card_count)
            self.encoder = CoreV4NumpyEncoder(card_vocabulary=vocabulary)

    # 原生快照保持原合同，Link 玩家快照通过独立兼容路径编码
    def encode(self, snapshot, player_id, *, output_format="numpy"):
        if not isinstance(snapshot, GameSnapshot):
            return self.encode_online(snapshot, player_id, output_format=output_format)
        if hasattr(snapshot, "_link_v4_projection"):
            raise ValueError("V4 在线诊断投影尚未验收，不能作为正常推理输入")
        with use_assets(self.assets):
            arrays = self.encoder.encode(snapshot, player_id)
        arrays = {key: np.ascontiguousarray(value) for key, value in arrays.items()}
        if output_format == "numpy":
            return arrays
        if output_format != "torch":
            raise ValueError("V4 编码输出必须是 numpy 或 torch")
        import torch
        return {key: torch.from_numpy(value) for key, value in arrays.items()}

    # 编码实际玩家观测并保存兼容报告，诊断投影不能直接用于推理
    def encode_online(self, snapshot, player_id, *, output_format="numpy", state_evaluation=False):
        from data_types import GameSnapshot as LinkSnapshot
        from .live_adapter import HISTORY_POLICY, ONLINE_MODE, prepare_online_input
        if not isinstance(snapshot, LinkSnapshot):
            raise ValueError("V4 在线兼容输入只接受 Link 对局玩家快照")
        try:
            prepared = prepare_online_input(snapshot, player_id, state_evaluation=state_evaluation)
        except ValueError as error:
            self.last_online_report = {"schema_version": "galatea.v4_online_input.v1", "mode": ONLINE_MODE,
                "history_policy": HISTORY_POLICY, "ready": False, "blocking_reasons": [str(error)]}
            raise
        self.last_online_report = prepared.report
        if prepared.snapshot is None:
            raise ValueError("V4 在线观测合同缺少可用输入: " + ", ".join(prepared.report["blocking_reasons"]))
        try:
            arrays = self.encode(prepared.snapshot, player_id, output_format="numpy")
        except (ValueError, TypeError, IndexError) as error:
            self.last_online_report = {**prepared.report, "ready": False,
                "blocking_reasons": ["encoding_failed"], "error": str(error)}
            raise
        if any(not np.isfinite(value).all() for value in arrays.values() if value.dtype.kind == "f"):
            self.last_online_report = {**prepared.report, "ready": False, "blocking_reasons": ["nonfinite_model_input"]}
            raise ValueError("V4 在线兼容输入包含非有限数值")
        if output_format == "numpy":
            return arrays
        if output_format != "torch":
            raise ValueError("V4 编码输出必须是 numpy 或 torch")
        import torch
        return {key: torch.from_numpy(value) for key, value in arrays.items()}

    # 仅对照诊断投影与参考快照，结果不包含输入数值且不执行网络推理
    def audit_projection(self, projection, reference, player_id, *, sample_limit=8):
        from .online_adapter import compare_encoded_observations
        if projection.snapshot is None or not hasattr(projection.snapshot, "_link_v4_projection"):
            raise ValueError("缺少已开始对局的诊断投影")
        if projection.report["perspective"] != player_id:
            raise ValueError("诊断视角不一致")
        if "fixed_capacity_exceeded" in projection.report["blocking_reasons"]:
            raise ValueError("诊断投影超出 V4 固定容量")
        if not isinstance(reference, GameSnapshot) or hasattr(reference, "_link_v4_projection"):
            raise ValueError("参考必须是独立的完整 V4 快照")
        with use_assets(self.assets):
            expected = self.encoder.encode(reference, player_id)
            actual = self.encoder.encode(projection.snapshot, player_id)
        report = compare_encoded_observations(expected, actual, sample_limit=sample_limit)
        report["source_readiness"] = projection.report
        return report
