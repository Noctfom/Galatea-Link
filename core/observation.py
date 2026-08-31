# LLM 可见观察模块，负责把对局快照转换为不泄露隐藏信息的 JSON 数据

import json
from collections import Counter
from typing import Any

from game_constants import Phases, Position, Zone
from utils.card_reader import card_db


SCHEMA_VERSION = "galatea.llm_observation.v1"

MESSAGE_NAMES = {
    10: "select_battle_command",
    11: "select_idle_command",
    12: "select_effect_yes_no",
    13: "select_yes_no",
    14: "select_option",
    15: "select_card",
    16: "select_chain",
    18: "select_place",
    19: "select_position",
    20: "select_tribute",
    22: "select_counter",
    23: "select_sum",
    24: "select_disabled_field",
    25: "sort_card",
    26: "select_unselect",
    40: "new_turn",
    41: "new_phase",
    50: "move",
    70: "chain_start",
    74: "chain_end",
    90: "draw",
    91: "damage",
    92: "recover",
    93: "equip",
    94: "lp_update",
    110: "attack",
    111: "battle",
    140: "announce_race",
    141: "announce_attribute",
    142: "announce_card",
    143: "announce_number",
}

IDLE_ACTION_NAMES = {
    0: "normal_summon",
    1: "special_summon",
    2: "change_position",
    3: "set_monster",
    4: "set_spell_trap",
    5: "activate",
    6: "enter_battle_phase",
    7: "enter_end_phase",
}

BATTLE_ACTION_NAMES = {
    0: "activate",
    1: "attack",
    2: "enter_main_phase_2",
    3: "enter_end_phase",
}

PROMPT_ACTION_NAMES = {
    12: "answer_effect_yes_no",
    13: "answer_yes_no",
    14: "choose_option",
    15: "select_card",
    16: "select_chain",
    18: "select_place",
    19: "select_position",
    20: "select_tribute",
    22: "select_counter",
    23: "select_sum",
    24: "select_disabled_field",
    25: "sort_card",
    26: "select_or_unselect",
    140: "announce_race",
    141: "announce_attribute",
    142: "announce_card",
    143: "announce_number",
}


class LlmObservationBuilder:
    # 初始化卡片信息来源和效果文本开关
    def __init__(self, card_reader=card_db, include_card_text: bool = True):
        self.card_reader = card_reader
        self.include_card_text = include_card_text

    # 从指定玩家视角构建完整可见观察
    def build(self, snapshot, player_id: int, event_type: int | None = None) -> dict[str, Any]:
        if player_id not in (0, 1):
            raise ValueError("玩家编号必须是 0 或 1")

        catalog: dict[int, dict[str, Any]] = {}
        index_to_entity_id: dict[int, str] = {}
        cards = []

        for index, entity in enumerate(snapshot.entities):
            entity_id = self._entity_id(entity, player_id)
            index_to_entity_id[index] = entity_id
            cards.append(self._build_entity(entity, entity_id, player_id, catalog))

        actor = self._relative_player(snapshot.global_data.to_play, player_id)
        legal_actions = []
        if actor == "self":
            legal_actions = [
                self._build_action(action, choice_id, event_type, index_to_entity_id, catalog)
                for choice_id, action in enumerate(snapshot.valid_actions)
            ]

        opponent_id = 1 - player_id
        known_hands = getattr(snapshot, "known_hand_codes", {})
        known_opponent_hand = self._summarize_codes(
            known_hands.get(opponent_id, []),
            catalog,
            include_in_catalog=True,
        )

        chain = [self._build_public_event(item, player_id, catalog) for item in snapshot.chain_stack]
        history = [self._build_public_event(item, player_id, catalog) for item in snapshot.history_stack]

        own_deck = snapshot.p0_deck_codes if player_id == 0 else snapshot.p1_deck_codes
        own_extra = snapshot.p0_extra_codes if player_id == 0 else snapshot.p1_extra_codes

        return {
            "schema_version": SCHEMA_VERSION,
            "information_quality": {
                "scope": "modeled_player_visible_state",
                "complete_protocol_projection": False,
                "limitations": [
                    "仅包含 DuelState 当前已经建模的协议字段",
                    "部分效果描述只有 description_id 而没有对应的选项文本",
                    "超量素材目前只提供数量而不提供逐张身份",
                    "持续效果、无效状态、回合限制和玩家提示尚未完整建模",
                    "近期历史只记录部分公开发动而不是完整效果结算过程",
                ],
            },
            "perspective": {
                "player_id": player_id,
                "opponent_player_id": opponent_id,
            },
            "event": {
                "type": event_type,
                "name": MESSAGE_NAMES.get(event_type, f"message_{event_type}") if event_type is not None else None,
            },
            "turn": {
                "number": snapshot.global_data.turn_count,
                "phase_id": snapshot.global_data.phase_id,
                "phase_name": Phases.get_str(snapshot.global_data.phase_id),
                "actor": actor,
            },
            "players": {
                "self": self._build_player_resources(snapshot.global_data, player_id),
                "opponent": self._build_player_resources(snapshot.global_data, opponent_id),
            },
            "cards": cards,
            "known_information": {
                "opponent_hand_unordered": known_opponent_hand,
                "own_remaining_deck": self._summarize_codes(own_deck, catalog, include_in_catalog=False),
                "own_remaining_extra": self._summarize_codes(own_extra, catalog, include_in_catalog=False),
            },
            "chain": chain,
            "recent_history": history,
            "decision_required": actor == "self" and bool(legal_actions),
            "legal_actions": legal_actions,
            "card_catalog": [catalog[code] for code in sorted(catalog)],
        }

    # 将观察转换为稳定可读的 JSON 文本
    def to_json(self, observation: dict[str, Any], indent: int | None = None) -> str:
        return json.dumps(observation, ensure_ascii=False, indent=indent, sort_keys=True)

    # 构建相对玩家资源统计
    def _build_player_resources(self, global_data, absolute_player_id: int) -> dict[str, int]:
        if absolute_player_id == 0:
            return {
                "player_id": 0,
                "lp": global_data.my_lp,
                "hand_count": global_data.my_hand_len,
                "deck_count": global_data.my_deck_len,
                "grave_count": global_data.my_grave_len,
                "removed_count": global_data.my_removed_len,
                "extra_count": global_data.my_extra_len,
            }
        return {
            "player_id": 1,
            "lp": global_data.op_lp,
            "hand_count": global_data.op_hand_len,
            "deck_count": global_data.op_deck_len,
            "grave_count": global_data.op_grave_len,
            "removed_count": global_data.op_removed_len,
            "extra_count": global_data.op_extra_len,
        }

    # 构建单张卡片的可见数据并物理移除隐藏身份
    def _build_entity(
        self,
        entity,
        entity_id: str,
        player_id: int,
        catalog: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        visible = self._is_identity_visible(entity, player_id)
        result = {
            "entity_id": entity_id,
            "controller": self._relative_player(entity.owner, player_id),
            "location_id": entity.location,
            "location_name": Zone.get_str(entity.location),
            "sequence": entity.sequence,
            "position_id": entity.position,
            "position_name": Position.get_str(entity.position),
            "visibility": "visible" if visible else "hidden",
            "overlay_count": entity.overlay_count,
            "counter_count": entity.counter_count,
            "is_equipped": entity.is_equipped,
        }
        if not visible:
            return result

        code = self._pure_code(entity.code)
        result.update(
            {
                "code": code,
                "current_atk": entity.current_atk,
                "current_def": entity.current_def,
                "level": entity.level,
                "type_mask": entity.type_mask,
                "race": entity.race,
                "attribute": entity.attribute,
            }
        )
        if code:
            card = self._register_card(code, catalog)
            result["name"] = card["name"]
        return result

    # 判断卡片身份是否对当前玩家可见
    def _is_identity_visible(self, entity, player_id: int) -> bool:
        if entity.owner == player_id:
            return True
        if entity.location == Zone.GRAVE:
            return True
        return bool(entity.is_public)

    # 构建可供 LLM 返回 choice_id 的合法动作
    def _build_action(
        self,
        action,
        choice_id: int,
        event_type: int | None,
        index_to_entity_id: dict[int, str],
        catalog: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        action_name = self._action_name(event_type, action.action_type)
        result = {
            "choice_id": choice_id,
            "action_type": action.action_type,
            "action_name": action_name,
            "description": action.desc_str or action_name,
        }

        target_index = getattr(action, "target_entity_idx", -1)
        if target_index in index_to_entity_id:
            result["target_entity_id"] = index_to_entity_id[target_index]

        macro_targets = getattr(action, "macro_targets", None) or []
        target_ids = [index_to_entity_id[index] for index in macro_targets if index in index_to_entity_id]
        if target_ids:
            result["target_entity_ids"] = target_ids

        macro_places = getattr(action, "macro_places", None) or []
        if macro_places:
            result["place_ids"] = list(macro_places)

        desc_id = getattr(action, "desc_id", 0)
        if desc_id:
            result["description_id"] = desc_id

        action_code = self._pure_code(getattr(action, "code", 0))
        if not action_code and event_type == 142:
            action_code = self._pure_code(desc_id)
        if action_code:
            result["card_code"] = action_code
            result["card_name"] = self._register_card(action_code, catalog)["name"]
        return result

    # 构建公开连锁与历史事件
    def _build_public_event(
        self,
        item: dict[str, Any],
        player_id: int,
        catalog: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        code = self._pure_code(item.get("code", 0))
        if code:
            result["code"] = code
            result["name"] = self._register_card(code, catalog)["name"]
        if "c" in item:
            result["controller"] = self._relative_player(item["c"], player_id)
        if "l" in item:
            result["location_id"] = item["l"]
            result["location_name"] = Zone.get_str(item["l"])
        if "s" in item:
            result["sequence"] = item["s"]
        if item.get("desc"):
            result["description_id"] = item["desc"]
        return result

    # 汇总已知卡片并保留重复数量
    def _summarize_codes(
        self,
        codes,
        catalog: dict[int, dict[str, Any]],
        include_in_catalog: bool,
    ) -> list[dict[str, Any]]:
        counts = Counter(self._pure_code(code) for code in codes if self._pure_code(code))
        result = []
        for code in sorted(counts):
            card = self._card_identity(code)
            if include_in_catalog:
                self._register_card(code, catalog)
            result.append({"code": code, "name": card["name"], "count": counts[code]})
        return result

    # 注册本次观察引用到的卡片说明
    def _register_card(self, code: int, catalog: dict[int, dict[str, Any]]) -> dict[str, Any]:
        if code not in catalog:
            catalog[code] = self._card_identity(code)
        return catalog[code]

    # 查询卡片名称和可选效果文本
    def _card_identity(self, code: int) -> dict[str, Any]:
        result = {"code": code, "name": self.card_reader.get_card_name(code)}
        if self.include_card_text:
            get_card_text = getattr(self.card_reader, "get_card_text", None)
            if get_card_text is not None:
                result["text"] = get_card_text(code)
        return result

    # 根据提示类型生成稳定动作名称
    def _action_name(self, event_type: int | None, action_type: int) -> str:
        if event_type == 11:
            return IDLE_ACTION_NAMES.get(action_type, f"idle_action_{action_type}")
        if event_type == 10:
            return BATTLE_ACTION_NAMES.get(action_type, f"battle_action_{action_type}")
        return PROMPT_ACTION_NAMES.get(event_type, f"action_{action_type}")

    # 生成不依赖绝对座位的实体编号
    def _entity_id(self, entity, player_id: int) -> str:
        controller = self._relative_player(entity.owner, player_id)
        return f"{controller}:{entity.location}:{entity.sequence}"

    # 将绝对座位转换为相对身份
    def _relative_player(self, absolute_player_id: int, player_id: int) -> str:
        return "self" if absolute_player_id == player_id else "opponent"

    # 清除卡密中的引擎标记位
    def _pure_code(self, code: int) -> int:
        return int(code or 0) & 0x7FFFFFFF
