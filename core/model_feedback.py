# 模型旁路反馈数据模块，保存真实输出和有界曲线且不参与游戏响应

from collections import deque
from copy import deepcopy
import math
import time

import numpy as np

MAX_REMAINING_EVENTS = 1500
MAX_DISPLAY_EVENTS = 1_000_000


# 将价值与独立终局头转成标量，不把折扣价值转换为胜率
def serialize_model_evaluation(values, predictions=None):
    array = np.asarray(values, dtype=np.float64).reshape(-1)
    value = float(array[0]) if len(array) == 1 and np.isfinite(array[0]) else None
    result = {"discounted_value": value, "probabilities": None, "remaining_events": None,
        "remaining_events_clipped": False, "length_unit": "transition_events",
        "calibration": "uncalibrated", "terminal_reason": "terminal_head_unavailable"}
    if not predictions:
        return result
    logits = np.asarray(predictions.get('terminal_outcome_logits'), dtype=np.float64)
    remaining = np.asarray(predictions.get('terminal_remaining'), dtype=np.float64).reshape(-1)
    if logits.shape != (1, 3) or len(remaining) != 1 or not np.isfinite(logits).all() or not np.isfinite(remaining).all():
        result['terminal_reason'] = 'invalid_terminal_output'
        return result
    probabilities = np.exp(logits[0] - logits[0].max())
    probabilities /= probabilities.sum()
    log_events = max(0.0, float(remaining[0])) * math.log1p(MAX_REMAINING_EVENTS)
    result.update(probabilities=dict(zip(('loss', 'draw', 'win'), map(float, probabilities))),
        remaining_events=math.expm1(min(log_events, math.log1p(MAX_DISPLAY_EVENTS))),
        remaining_events_clipped=log_events > math.log1p(MAX_DISPLAY_EVENTS), terminal_reason=None)
    return result


class ModelFeedbackHistory:
    # 限制每局采样数量，状态仅保存预测数值而不保存模型输入或卡片内容
    def __init__(self):
        self.points = deque(maxlen=128)
        self.epoch = 0
        self.last_error = None

    # 重置单局或重建边界，已经启动的旧任务通过代次检查失效
    def reset(self, *, clear=True):
        self.epoch += 1
        self.last_error = None
        if clear:
            self.points.clear()

    # 记录带来源与视角的真实预测，同一状态优先保留决策共用采样
    def record(self, evaluation, snapshot, model, *, source, request_id=None):
        coverage = getattr(snapshot, 'online_coverage', {})
        point = {**deepcopy(evaluation), 'sampled_at': time.time(), 'source': source,
            'request_id': request_id, 'state_sequence': coverage.get('sequence'),
            'perspective': coverage.get('perspective', snapshot.global_data.to_play),
            'turn': snapshot.global_data.turn_count, 'phase_id': snapshot.global_data.phase_id,
            'model': {key: model.get(key) for key in ('model_id', 'iteration', 'model_protocol_version', 'inference_backend')},
            'history_policy': 'masked_unavailable', 'native_observation_complete': False}
        if self.points and self.points[-1]['state_sequence'] == point['state_sequence']:
            if source == 'passive' and self.points[-1]['source'] == 'decision_shared':
                return None
            self.points.pop()
        self.points.append(point)
        self.last_error = None
        return deepcopy(point)

    # 返回独立副本，明确最近采样是否仍对应当前状态
    def snapshot(self, *, state_sequence=None, active=False, enabled=True, passive_enabled=False):
        points = deepcopy(list(self.points))
        latest = points[-1] if points else None
        return {'schema_version': 'galatea.model_feedback.v1', 'enabled': enabled,
            'passive_enabled': passive_enabled, 'available': latest is not None,
            'scope': 'current_duel' if active else 'last_duel',
            'current': bool(active and latest and latest['state_sequence'] == state_sequence),
            'latest': latest, 'history': points, 'capacity': 128, 'last_error': self.last_error,
            'limitations': ['player_visible_only', 'uncalibrated_predictions', 'transition_history_masked']}
