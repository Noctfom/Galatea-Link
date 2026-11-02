# V4 编码器运行包装，纯 NumPy 构造完整离线快照输入并拒绝旧在线快照

import numpy as np

from .data_types import GameSnapshot
from .feature_encoder import CoreV4NumpyEncoder
from .runtime_context import use_assets


class GalateaEncoder:
    # 为单个模型绑定独立词表语义和静态卡组缓存
    def __init__(self, assets):
        self.assets = assets
        with use_assets(assets):
            self.encoder = CoreV4NumpyEncoder(card_vocabulary=assets.vocabulary)

    # 仅编码 V4 完整快照且不接受当前 Link 的不完整在线状态
    def encode(self, snapshot, player_id, *, output_format="numpy"):
        if not isinstance(snapshot, GameSnapshot):
            raise ValueError("V4 在线观测合同尚未验收，不能将旧在线快照补零编码")
        with use_assets(self.assets):
            arrays = self.encoder.encode(snapshot, player_id)
        arrays = {key: np.ascontiguousarray(value) for key, value in arrays.items()}
        if output_format == "numpy":
            return arrays
        if output_format != "torch":
            raise ValueError("V4 编码输出必须是 numpy 或 torch")
        import torch
        return {key: torch.from_numpy(value) for key, value in arrays.items()}
