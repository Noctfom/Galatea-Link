# Link 在线状态到 V4 的诊断映射，保留来源缺口并禁止作为完整推理输入

from copy import deepcopy
from dataclasses import dataclass, fields, replace
from types import SimpleNamespace

from .data_types import CardEntity, GameAction, GameSnapshot, GlobalFeature, CardRelationType
from .data_types import ActionOperation, SummonMethod, TransitionBoundary
from .event_history import TransitionEventRecorder, TransitionStateDigest, validate_transition_history
from .game_constants import LocationInfo, Zone


CARD_QUERY_FIELDS = frozenset({"code", "position", "current_code", "current_type", "current_level",
    "current_rank", "current_attr", "current_race", "current_atk", "current_def", "base_atk", "base_def",
    "reason", "reason_card", "equip_target", "target_locations", "overlay_codes", "counter_items",
    "original_owner", "status_mask", "lscale", "rscale", "link_rating", "link_marker"})
IDLE_OPERATIONS = (ActionOperation.NORMAL_SUMMON, ActionOperation.SPECIAL_SUMMON,
    ActionOperation.CHANGE_POSITION, ActionOperation.MONSTER_SET, ActionOperation.SPELL_TRAP_SET, ActionOperation.ACTIVATE)
PROMPT_TYPES = frozenset({10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26, 140, 141, 142, 143})


@dataclass(frozen=True)
class V4OnlineProjection:
    snapshot: GameSnapshot | None
    report: dict


# 只按 dataclass 明确声明的字段复制，列表不引用实时状态
def copy_fields(source, destination):
    return {field.name: deepcopy(getattr(source, field.name)) for field in fields(destination)
        if hasattr(source, field.name)}


# 从实际交互分组补齐 V4 操作语义，不改变候选次序或响应字节
def action_values(action, event_type):
    values = copy_fields(action, GameAction)
    raw_code = int(values.get("code", 0))
    values["code"] = raw_code & 0x7FFFFFFF
    if event_type == 11 and 0 <= action.action_type <= 5:
        values["operation_id"] = int(IDLE_OPERATIONS[action.action_type])
        values["summon_method_id"] = int(SummonMethod.NORMAL if action.action_type == 0 else SummonMethod.SPECIAL_UNKNOWN if action.action_type == 1 else SummonMethod.NONE)
        values["prompt_flags"] = int(bool(raw_code & 0x80000000))
    if values.get("response_value") is None:
        if event_type in (10, 11):
            values["response_value"] = int((action.index << 16) | action.action_type)
        elif event_type in (12, 13, 14, 16, 19):
            # 原生连锁取消保留未设置的响应特征，发包仍按候选索引返回负一
            if not (event_type == 16 and action.index == -1):
                values["response_value"] = int(action.index)
        elif event_type in (140, 141, 142, 143):
            values["response_value"] = int(action.desc_id)
    return values


# 将本方可见响应区间转成可对照事件，不伪造对手动作或完整训练边界
def project_own_history(records, player):
    recorder = TransitionEventRecorder()
    events, skipped = [], 0
    for record in records:
        if record.actor != player or record.action is None or record.truncated:
            skipped += 1
            continue
        before = TransitionStateDigest(record.before.lp_p0, record.before.lp_p1,
            record.before.zone_counts, record.before.chain_depth)
        after = TransitionStateDigest(record.after.lp_p0, record.after.lp_p1,
            record.after.zone_counts, record.after.chain_depth)
        action = SimpleNamespace(**action_values(SimpleNamespace(**dict(record.action)), record.prompt_type))
        recorder.begin(actor=player, turn_count=record.turn, phase_id=record.phase,
            prompt_type=record.prompt_type, action=action, start_digest=before,
            source_visibility_mask=1 << player, target_visibility_mask=1 << player)
        for kind, payload, depth in record.messages:
            recorder.observe_message(kind, payload, depth)
        recorder.mark_boundary({"next_own_prompt": TransitionBoundary.NEXT_DECISION,
            "retry": TransitionBoundary.RETRY, "terminal": TransitionBoundary.TERMINAL}[record.boundary])
        event = recorder.finalize(after)
        events.append(replace(event, sequence_id=record.sequence, event_visibility_mask=1 << player,
            intent_visibility_mask=1 << player, source_visibility_mask=1 << player,
            target_visibility_mask=1 << player))
    validate_transition_history(events)
    return events, skipped


# 按模型输入名称分组，不改变字段或构造另一套网络协议
def input_group(name):
    if name in {"global", "phase", "player_context", "field_zone_mask"}:
        return "global"
    for prefix, group in (("card_", "cards"), ("deck_", "deck"), ("c_", "chain"),
            ("h_", "effect_history"), ("event_", "transitions"), ("act_", "actions"),
            ("hint_", "public_context"), ("known_deck_", "public_context")):
        if name.startswith(prefix):
            return group
    return "cards" if name == "padding_mask" else "public_context" if name == "public_state_flags" else "unclassified"


# 对照编码只返回字段形状类型和差异索引，不输出卡密数值或原始响应
def compare_encoded_observations(expected, actual, *, sample_limit=8):
    import numpy as np
    if type(sample_limit) is not int or not 0 <= sample_limit <= 32:
        raise ValueError("差异样本数量必须为 0 到 32")
    report = []
    for name in sorted(set(expected) | set(actual)):
        item = {"field": name, "group": input_group(name)}
        if name not in expected or name not in actual:
            item.update(equal=False, reason="missing_field")
        else:
            left, right = np.asarray(expected[name]), np.asarray(actual[name])
            item.update(shape=list(left.shape), dtype=str(left.dtype))
            if left.shape != right.shape or left.dtype != right.dtype:
                item.update(equal=False, reason="layout_mismatch", actual_shape=list(right.shape), actual_dtype=str(right.dtype))
            else:
                equal = bool(np.isfinite(left).all() and np.isfinite(right).all() and np.array_equal(left, right))
                item.update(equal=equal, reason="equal" if equal else "value_mismatch")
                if not equal:
                    indices = np.argwhere(left != right)
                    item.update(different_elements=int(len(indices)), sample_indices=indices[:sample_limit].tolist())
        report.append(item)
    return {"field_count": len(report), "matching_fields": sum(item["equal"] for item in report),
        "different_fields": [item["field"] for item in report if not item["equal"]],
        "all_fields_equal": bool(report) and all(item["equal"] for item in report),
        "fields": report, "diagnostic_only": True, "grants_inference_permission": False}


# 映射当前在线快照并明确标出缺失字段，诊断默认值不升级为模型事实
def project_online_snapshot(source, player, *, event_type=None, history_projection=None):
    if player not in (0, 1):
        raise ValueError("V4 映射需要有效物理座位")
    coverage = deepcopy(getattr(source, "online_coverage", {}))
    if coverage.get("perspective") not in (None, player):
        raise ValueError("V4 映射不能切换消息接收者")
    report = {"schema_version": "galatea.v4_observation_diagnostics.v1", "available": bool(coverage.get("started")),
        "perspective": player, "sequence": coverage.get("sequence"), "diagnostic_only": True,
        "online_inference_ready": False, "behavior_clone_eligible": False, "groups": {}, "blocking_reasons": []}
    report["source"] = {"query_mode": coverage.get("query_mode"),
        "query_contract_confirmed": coverage.get("query_cache_contract_confirmed", False),
        "public_messages_received": coverage.get("public_messages_received", {})}
    if not report["available"]:
        report["blocking_reasons"] = ["start_missing"]
        return V4OnlineProjection(None, report)
    global_values = copy_fields(source.global_data, GlobalFeature)
    for name in ("decision_player", "turn_player", "starting_player"):
        if global_values.get(name) is None:
            global_values[name] = -1
    global_data = GlobalFeature(**global_values)
    global_complete = coverage.get("public_count_baseline_valid", False) and all(getattr(global_data, key) in (0, 1) for key in ("decision_player", "turn_player", "starting_player"))
    report["groups"]["global"] = {"mapped": True, "source_complete": global_complete}
    entities, old_to_new, missing_cards = [], {}, []
    for old_index, entity in enumerate(source.entities):
        if entity.location == Zone.EXTRA and getattr(entity, "query_inventory", False):
            continue
        values = copy_fields(entity, CardEntity)
        values["relation_indices"], values["relation_types"] = [], []
        # V4 将装备来源和被装备分开，接收方由下方反向关系证明
        values["is_equipped"] = False
        values["rank"] = getattr(entity, "current_rank", 0)
        visible = entity.owner == player or entity.is_public or entity.location == Zone.GRAVE
        public_flags = getattr(source, "public_state_flags", ())
        if entity.location == Zone.GRAVE and len(public_flags) >= 5 and public_flags[3 + player]:
            visible = False
        if not visible:
            values = {key: value for key, value in values.items() if key in {"owner", "location", "sequence", "position"}}
            values.update(code=0, current_atk=0, current_def=0, type_mask=0, race=0, attribute=0,
                level=0, base_atk=0, base_def=0, is_public=False)
        else:
            known = set(getattr(entity, "query_known_fields", ()) or ())
            known.update(getattr(entity, "relation_known_fields", ()))
            missing = sorted(CARD_QUERY_FIELDS - known)
            if missing:
                missing_cards.append({"player": entity.owner, "zone": entity.location,
                    "sequence": entity.sequence, "fields": missing})
        old_to_new[old_index] = len(entities)
        entities.append(CardEntity(**values))
    positions = {(entity.owner, entity.location, entity.sequence): index for index, entity in enumerate(entities)}
    # 在完整索引建成后解析正向和反向位置关系
    for source_index, entity in enumerate(entities):
        for relation_type, raw in [(CardRelationType.REASON_CARD, entity.reason_card_location),
                (CardRelationType.EQUIP_TARGET, entity.equip_target_location),
                *((CardRelationType.EFFECT_TARGET, raw) for raw in entity.target_locations)]:
            if not raw:
                continue
            target = positions.get(LocationInfo.decode(raw)[:3])
            if target is None:
                continue
            pair = (int(relation_type), target)
            if pair not in set(zip(entity.relation_types, entity.relation_indices)):
                entity.relation_types.append(pair[0])
                entity.relation_indices.append(pair[1])
            if relation_type == CardRelationType.EQUIP_TARGET:
                receiver = entities[target]
                receiver.is_equipped = True
                receiver.relation_types.append(int(CardRelationType.EQUIP_SOURCE))
                receiver.relation_indices.append(source_index)
    report["groups"]["cards"] = {"mapped": True, "source_complete": not missing_cards,
        "entity_count": len(entities), "missing_attributes": missing_cards[:120],
        "missing_card_count": len(missing_cards), "excluded_extra_inventory": len(source.entities) - len(entities)}
    actions, invalid_actions = [], []
    prompt_type = event_type if event_type in PROMPT_TYPES else getattr(source, "action_prompt_type", None)
    for index, action in enumerate(source.valid_actions):
        values = action_values(action, prompt_type)
        old_target = values.get("target_entity_idx", -1)
        if old_target >= len(source.entities) or old_target < -1:
            invalid_actions.append(index)
        values["target_entity_idx"] = old_to_new.get(old_target, -1)
        if values.get("macro_targets") is not None:
            values["macro_targets"] = [old_to_new.get(item, -1) for item in values["macro_targets"]]
        mapped_action = GameAction(**values)
        mapped_action.explicit_response = bool(getattr(action, "explicit_response", False))
        if hasattr(action, "sort_prompt_indices"):
            mapped_action.sort_prompt_indices = list(action.sort_prompt_indices)
        actions.append(mapped_action)
    chain_unverified = prompt_type == 16 and coverage.get("chain_entry_size") != 14
    report["groups"]["actions"] = {"mapped": True, "source_complete": not invalid_actions and prompt_type in PROMPT_TYPES and not chain_unverified,
        "legacy_chain_semantics_unverified": chain_unverified,
        "candidate_count": len(actions), "invalid_target_candidates": invalid_actions[:120]}
    if prompt_type in (15, 18, 20, 21, 22, 23, 24, 25, 140, 141):
        explicit = bool(actions) and all(getattr(action, "explicit_response", False) or action.decision_bytes
            or action.decision_value is not None for action in actions)
        report["groups"]["actions"]["candidate_stage"] = "explicit_responses" if explicit else "base_candidates"
    else:
        report["groups"]["actions"]["candidate_stage"] = "direct_candidates"
    report["groups"]["actions"]["selection_response_checks"] = prompt_type in (15, 20, 21, 22, 23, 25, 26)
    own_deck_known = bool(getattr(source, "initial_deck_known", {}).get(player))
    own_deck_known = own_deck_known and len(getattr(source, f"p{player}_deck_codes", ())) == (global_data.my_deck_len if player == 0 else global_data.op_deck_len)
    own_deck_known = own_deck_known and len(getattr(source, f"p{player}_extra_codes", ())) == (global_data.my_extra_len if player == 0 else global_data.op_extra_len)
    report["groups"]["deck"] = {"mapped": True, "source_complete": own_deck_known}
    history, skipped = history_projection if history_projection is not None else project_own_history(getattr(source, "observed_transitions", ()), player)
    report["groups"]["transitions"] = {"mapped": True, "source_complete": False,
        **deepcopy(getattr(source, "observed_history_coverage", {})), "mapped_records": len(history), "skipped_records": skipped}
    report["groups"]["public_context"] = {"mapped": True, "source_complete": False,
        "tracked_lifecycle_consistent": bool(getattr(source, "public_state_flags", (False,))[0]),
        "hint_count": len(getattr(source, "public_hints", ())) }
    report["groups"]["chain"] = {"mapped": True, "source_complete": False}
    report["groups"]["effect_history"] = {"mapped": True, "source_complete": False}
    target = GameSnapshot(global_data, entities, actions, chain_stack=deepcopy(source.chain_stack),
        history_stack=deepcopy(source.history_stack), transition_history=history,
        public_hints=deepcopy(getattr(source, "public_hints", ())),
        known_deck_cards=deepcopy(getattr(source, "known_deck_cards", ())),
        public_state_flags=tuple(getattr(source, "public_state_flags", (False,) * 6)))
    for section in ("deck", "extra", "initial_deck", "initial_extra"):
        name = f"p{player}_{section}_codes"
        setattr(target, name, list(getattr(source, name, ())))
    report["blocking_reasons"] = [f"{name}_source_incomplete" for name, group in report["groups"].items() if not group["source_complete"]]
    if len(entities) > 120 or len(actions) > 120:
        report["blocking_reasons"].append("fixed_capacity_exceeded")
    report["blocking_reasons"].append("live_model_validation_pending")
    target._link_v4_projection = True
    return V4OnlineProjection(target, report)


class V4OnlineProjector:
    # 每局独立缓存已完成的不可变历史，不在进程全局保存玩家信息
    def __init__(self):
        self.reset()

    # 清理本局引用且不影响已经导出的匿名报告
    def reset(self):
        self._records = None
        self._player = None
        self._history = None

    # 历史未变化时复用转换结果，避免每个网络消息重复回放整段历史
    def project(self, source, player, *, event_type=None):
        records = tuple(getattr(source, "observed_transitions", ()))
        unchanged = self._records is not None and len(records) == len(self._records) and all(
            left is right for left, right in zip(records, self._records))
        if self._player != player or not unchanged:
            self._history = project_own_history(records, player)
            self._records, self._player = records, player
        return project_online_snapshot(source, player, event_type=event_type, history_projection=self._history)
