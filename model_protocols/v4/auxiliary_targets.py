# V4 辅助头推理所需的维度与枚举，固定复用 Core V4 协议且不依赖外部 Core 目录

from enum import IntEnum
from .event_history import TRANSITION_ZONE_NAMES

AUXILIARY_TARGET_FORMAT_VERSION = 3

PLAN_PAIR_ROLE_NONE = 0

PLAN_PAIR_ROLE_FIRST = 1

PLAN_PAIR_ROLE_SECOND = 2

class AuxiliaryHorizon(IntEnum):
    """定义辅助标签采用的四个未来时间尺度"""

    NEXT_DECISION = 0
    CHAIN_END = 1
    TURN_END = 2
    TERMINAL = 3

AUXILIARY_HORIZON_COUNT = len(AuxiliaryHorizon)

AUXILIARY_LP_DIM = 2

AUXILIARY_ZONE_DIM = 2 * len(TRANSITION_ZONE_NAMES)

AUXILIARY_CHAIN_DIM = 3
