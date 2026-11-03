# V4 编码器运行包装，纯 NumPy 构造完整离线快照输入并拒绝旧在线快照

import numpy as np

from .data_types import GameSnapshot
from .feature_encoder import CoreV4NumpyEncoder
from .runtime_context import use_assets


class GalateaEncoder:
    # 为单个模型绑定独立词表语义和静态卡组缓存
    def __init__(self, assets, *, frozen_card_count=None):
        self.assets = assets
        with use_assets(assets):
            vocabulary = assets.vocabulary if frozen_card_count is None else assets.vocabulary.prefix(frozen_card_count)
            self.encoder = CoreV4NumpyEncoder(card_vocabulary=vocabulary)

    # 仅编码 V4 完整快照且不接受当前 Link 的不完整在线状态
    def encode(self, snapshot, player_id, *, output_format="numpy"):
        if not isinstance(snapshot, GameSnapshot):
            raise ValueError("V4 在线观测合同尚未验收，不能将旧在线快照补零编码")
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
