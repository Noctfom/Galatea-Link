# V4 在线兼容输入，使用玩家可见状态并屏蔽无法可靠取得的原生转移历史

from copy import deepcopy
from dataclasses import dataclass

from .online_adapter import project_online_snapshot
from .data_types import CardEntity
from .game_constants import Zone
from .feature_encoder import MAX_CARDS, MAX_ACTIONS


ONLINE_MODE = "player_view_compatible_v1"
HISTORY_POLICY = "masked_unavailable"


@dataclass(frozen=True)
class CompatibleOnlineInput:
    snapshot: object | None
    report: dict


# 根据实际对局快照构造兼容输入，不要求在线消息重现 Core 双方决策边界
def prepare_online_input(source, player, *, state_evaluation=False):
    if state_evaluation:
        # 状态评测只使用场面，清空菜单而不虚构可行动作
        source = deepcopy(source)
        source.valid_actions = []
        source.action_prompt_type = None
    projection = project_online_snapshot(source, player, history_projection=([], 0))
    native_report = projection.report
    report = {"schema_version": "galatea.v4_online_input.v1", "mode": ONLINE_MODE,
        "history_policy": HISTORY_POLICY, "perspective": player,
        "sequence": native_report.get("sequence"), "ready": False,
        "native_observation_complete": False, "behavior_clone_eligible": False,
        "purpose": "state_evaluation" if state_evaluation else "decision",
        "blocking_reasons": [], "warnings": ["transition_history_masked"],
        "native_input_gaps": list(native_report["blocking_reasons"])}
    target = projection.snapshot
    if target is None:
        report["blocking_reasons"].append("start_missing")
        return CompatibleOnlineInput(None, report)
    if not state_evaluation and source.global_data.to_play != player:
        report["blocking_reasons"].append("decision_player_mismatch")
    if not state_evaluation and not target.valid_actions:
        report["blocking_reasons"].append("legal_actions_missing")
    if len(target.entities) > MAX_CARDS:
        report["blocking_reasons"].append("card_capacity_exceeded")
    if native_report["groups"]["actions"]["invalid_target_candidates"]:
        report["blocking_reasons"].append("invalid_action_target")
    if report["blocking_reasons"]:
        return CompatibleOnlineInput(None, report)
    # 手牌与额外区的表示字节不能证明对手卡片公开，兼容路径再次清理隐藏占位
    private_positions = set()
    for index, entity in enumerate(target.entities):
        hidden_zone = entity.location in (Zone.HAND, Zone.DECK, Zone.EXTRA)
        hidden_position = entity.location in (Zone.MZONE, Zone.SZONE, Zone.REMOVED) and not entity.position & 5
        if entity.owner != player and (hidden_zone or hidden_position):
            private_positions.add((entity.owner, entity.location, entity.sequence))
            target.entities[index] = CardEntity(code=0, owner=entity.owner, location=entity.location,
                sequence=entity.sequence, position=entity.position, current_atk=0, current_def=0,
                type_mask=0, race=0, attribute=0, level=0, base_atk=0, base_def=0, is_public=False)
    # 诊断的完整性标准保留，兼容推理只报告信息缺口而不冒充完整原生输入
    report["warnings"].extend(reason for reason in report["native_input_gaps"]
        if reason not in ("transitions_source_incomplete", "live_model_validation_pending", "fixed_capacity_exceeded"))
    if len(target.valid_actions) > MAX_ACTIONS:
        report["warnings"].append("action_capacity_truncated")
    report["missing_dynamic_card_count"] = sum((item["player"], item["zone"], item["sequence"]) not in private_positions
        for item in native_report["groups"]["cards"]["missing_attributes"])
    report["candidate_count"] = len(target.valid_actions)
    report["encoded_candidate_count"] = min(MAX_ACTIONS, len(target.valid_actions))
    report["query_contract_confirmed"] = native_report["source"]["query_contract_confirmed"]
    if not report["query_contract_confirmed"]:
        report["warnings"].append("query_cache_contract_unconfirmed")
    # 两类近期公开记录继续沿用已收到的事实，无法对齐的转移历史全部掩蔽
    target.transition_history = []
    target.known_hand_codes = deepcopy(getattr(source, "known_hand_codes", {0: [], 1: []}))
    delattr(target, "_link_v4_projection")
    target._link_v4_online_input = {"mode": ONLINE_MODE, "history_policy": HISTORY_POLICY}
    report["ready"] = True
    return CompatibleOnlineInput(target, deepcopy(report))
