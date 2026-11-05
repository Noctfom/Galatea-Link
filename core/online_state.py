# 在线 Start 与查询刷新投影，独立于模型版本并保留未知字段和可见性边界

from copy import deepcopy
from collections import deque
from dataclasses import replace
import struct

from core import query_protocol as q
from core.public_context import OnlinePublicContext, card_hint_has_host, location_key
from core.protocol_warnings import LOGGER
from core.confirmation_protocol import parse_online_confirmation
from game_constants import LocationInfo, Zone


ZONES = {1, 2, 4, 8, 16, 32, 64}
# 传统服务器会回传请求掩码中的此历史位，但 get_infos 不为它序列化字段
LEGACY_SERVER_RESERVED_QUERY_FLAGS = 0x100000
SERVER_CARD_QUERY_FLAGS = q.PUBLIC_CARD_QUERY_FLAGS | LEGACY_SERVER_RESERVED_QUERY_FLAGS
QUERY_FIELD_NAMES = {
    q.QUERY_CODE: ("code",), q.QUERY_POSITION: ("position",),
    q.QUERY_ALIAS: ("current_code",), q.QUERY_TYPE: ("current_type",),
    q.QUERY_LEVEL: ("current_level",), q.QUERY_RANK: ("current_rank",),
    q.QUERY_ATTRIBUTE: ("current_attr",), q.QUERY_RACE: ("current_race",),
    q.QUERY_ATTACK: ("current_atk",), q.QUERY_DEFENSE: ("current_def",),
    q.QUERY_BASE_ATTACK: ("base_atk",), q.QUERY_BASE_DEFENSE: ("base_def",),
    q.QUERY_REASON: ("reason",), q.QUERY_REASON_CARD: ("reason_card",),
    q.QUERY_EQUIP_CARD: ("equip_target", "is_equip_source"),
    q.QUERY_TARGET_CARD: ("target_locations",), q.QUERY_OVERLAY_CARD: ("overlay_codes",),
    q.QUERY_COUNTERS: ("counter_items", "counter_count"), q.QUERY_OWNER: ("original_owner",),
    q.QUERY_STATUS: ("status_mask", "properly_summoned", "is_disabled", "is_forbidden"),
    q.QUERY_LSCALE: ("lscale",), q.QUERY_RSCALE: ("rscale",), q.QUERY_LINK: ("link_rating", "link_marker"),
}


class OnlineStateProjection:
    # 初始化单局玩家视角且默认不声明已经确认服务器缓存合同
    def __init__(self, *, query_mode=None):
        if query_mode not in {None, "replace", "merge"}:
            raise ValueError("在线查询缓存模式无效")
        self.query_mode = query_mode
        self.perspective = None
        self.counts = {}
        self.cards = {}
        self.sequence = 0
        self.started = False
        self.public_context = OnlinePublicContext()
        self.public_notifications = deque(maxlen=16)
        self.turn = 0
        self.turn_player = None
        self.starting_player = None
        self.turn_counts = [0, 0]
        self.disabled_field_mask = 0
        self.public_message_counts = {120: 0, 160: 0, 165: 0}
        self.last_known_draws = ()
        self.invalidated_locations = ()
        self.compatibility_warning_counts = {}
        self.compatibility_warnings = deque(maxlen=16)
        self.confirmation_header_size = None
        self.chain_entry_size = None
        self.field_rebuilds = 0
        self.last_field_rebuild = None

    # 记录可兼容差异并限制重复日志数量，不包含卡号聊天或原始载荷
    def warn_compatibility(self, code, **details):
        count = self.compatibility_warning_counts.get(code, 0) + 1
        self.compatibility_warning_counts[code] = count
        self.compatibility_warnings.append({"code": code, "occurrences": count, **details})
        if count <= 3 or count % 100 == 0:
            LOGGER.warning("在线协议兼容警告 %s count=%s %s", code, count, details)

    # 严格读取 Start 的座位生命值及区域数量
    def start(self, payload):
        if self.started:
            self.warn_compatibility("start_repeated")
            return None
        if len(payload) != 18:
            raise ValueError(f"在线 Start 载荷必须为 18 字节，实际 {len(payload)}")
        player, rule, lp0, lp1, main0, extra0, main1, extra1 = struct.unpack("<BBIIHHHH", payload)
        if player not in (0, 1):
            raise ValueError("在线 Start 玩家视角无效")
        if any(count > 256 for count in (main0, extra0, main1, extra1)):
            self.warn_compatibility("start_large_deck_counts", counts=(main0, extra0, main1, extra1))
        self.perspective = player
        self.cards.clear()
        self.counts = {(0, 1): main0, (0, 64): extra0, (1, 1): main1, (1, 64): extra1}
        self.public_context = OnlinePublicContext((main0, main1))
        self.started = True
        self.sequence += 1
        return {"perspective": player, "rule": rule, "lp": (lp0, lp1)}

    # 按行头确定记录边界，兼容未知附加字段且只解析已知字段
    def _rows(self, data, *, source=None):
        rows, offset = [], 0
        while offset < len(data):
            if len(data) - offset < 4:
                raise ValueError("在线查询行头截断")
            size = struct.unpack_from("<I", data, offset)[0]
            if (size != 4 and size < 8) or offset + size > len(data):
                raise ValueError(f"在线查询行长度无效或截断: row_index={len(rows)} row_bytes={size} remaining_bytes={len(data) - offset}")
            if size == 4:
                rows.append(None)
            elif size > 8 and not any(data[offset + 4:offset + size]):
                # YGOPro 将不可见查询行的长度头之后全部清零，保留占位并清除旧字段
                rows.append({"query_flags": 0, "redacted": True})
            else:
                flags = struct.unpack_from("<I", data, offset + 4)[0]
                unsupported = flags & ~SERVER_CARD_QUERY_FLAGS
                details = {**(source or {}), "row_index": len(rows), "row_bytes": size, "query_flags": f"0x{flags:08X}"}
                if unsupported:
                    self.warn_compatibility("query_unknown_flags", **details, unsupported_flags=f"0x{unsupported:08X}")
                try:
                    row = q.parse_legacy_card_query(data[offset:offset + size], allow_trailing_data=True)
                except q.CoreQueryError:
                    # 查询记录已有独立边界，字段无法解码时保留未知占位而不结束对局
                    self.warn_compatibility("query_row_unparsed", **details)
                    row = {"query_flags": 0, "redacted": True}
                else:
                    if row["query_trailing_bytes"]:
                        self.warn_compatibility("query_extra_bytes", **details, ignored_bytes=row["query_trailing_bytes"])
                    if flags == 0 or flags & q.QUERY_CODE and row["code"] == 0:
                        row["redacted"] = True
                rows.append(row)
            offset += size
        return rows

    # 将刷新映射为有来源的公开字段并在整包验证后更新状态
    def refresh(self, kind, payload):
        if not self.started:
            self.warn_compatibility("query_before_start", msg_type=kind)
            return None
        header_size = 2 if kind == 6 else 3
        if kind not in (6, 7) or len(payload) < header_size:
            raise ValueError("在线查询刷新头截断")
        actor, zone = payload[:2]
        if actor not in (0, 1) or zone not in ZONES:
            self.warn_compatibility("query_unsupported_region", player=actor, zone=zone)
            return None
        try:
            rows = self._rows(payload[header_size:], source={"msg_type": kind, "player": actor, "zone": zone})
        except ValueError as error:
            error.add_note(f"YGOPro 卡片状态查询: msg_type={kind} player={actor} zone=0x{zone:02X}")
            raise
        if kind == 7 and len(rows) != 1:
            self.warn_compatibility("query_single_row_count", rows=len(rows))
            rows = rows[:1]
        if kind == 6 and zone in (4, 8) and len(rows) != (7 if zone == 4 else 8):
            self.warn_compatibility("query_zone_slot_count", player=actor, zone=zone, rows=len(rows))
        if zone not in (4, 8) and any(row is None for row in rows):
            self.warn_compatibility("query_continuous_empty_slot", player=actor, zone=zone)
        indices = range(len(rows)) if kind == 6 else [payload[2]]
        if kind == 7 and (zone in (4, 8) and payload[2] >= (7 if zone == 4 else 8)):
            self.warn_compatibility("query_extended_slot", player=actor, zone=zone, index=payload[2])
        staged = {}
        for index, row in zip(indices, rows):
            key = actor, zone, index
            if row is None:
                staged[index] = None
                continue
            flags = row["query_flags"]
            if flags & q.QUERY_POSITION and (row["controller"], row["location"], row["sequence"]) != key:
                self.warn_compatibility("query_position_mismatch", player=actor, zone=zone, index=index)
            fields = {name: deepcopy(row[name]) for flag, names in QUERY_FIELD_NAMES.items() if flags & flag for name in names}
            if "code" in fields:
                fields["code"] &= 0x7FFFFFFF
            previous = self.cards.get(key, {})
            if not row.get("redacted") and self.query_mode == "merge" and ("code" not in fields or fields["code"] == previous.get("code")):
                fields = {**deepcopy(previous), **fields}
            position = fields.get("position", 0)
            hidden = zone == 1 or actor != self.perspective and (zone == 2 or zone != 16 and not position & 5)
            if hidden:
                fields = {"position": position} if position else {}
            fields["query_sequence"] = self.sequence + 1
            staged[index] = fields
        if kind == 6:
            self.cards = {key: value for key, value in self.cards.items() if key[:2] != (actor, zone)}
            self.counts[(actor, zone)] = sum(row is not None for row in rows)
            if zone == Zone.DECK:
                count = self.counts[(actor, zone)]
                if self.public_context.deck_counts[actor] != count:
                    self.public_context.deck_cards[actor].clear()
                    self.public_context.shuffle_hints(actor, Zone.DECK)
                self.public_context.deck_counts[actor] = count
        for index, fields in staged.items():
            key = actor, zone, index
            if fields is None:
                self.cards.pop(key, None)
            else:
                self.cards[key] = fields
        self.sequence += 1
        return actor, zone, staged

    # 报告已知边界而不把诊断投影宣称为完整 V4 输入
    def coverage(self):
        return {"visibility": "player_view", "query_mode": self.query_mode or "unconfirmed",
                "confirmation_header_size": self.confirmation_header_size,
                "chain_entry_size": self.chain_entry_size,
                "field_rebuilds": self.field_rebuilds,
                "last_field_rebuild": deepcopy(self.last_field_rebuild),
                "query_cache_contract_confirmed": self.query_mode is not None,
                "perspective": self.perspective, "started": self.started, "sequence": self.sequence,
                "public_count_baseline_valid": self.started and all((actor, zone) in self.counts for actor in (0, 1) for zone in (1, 64)),
                "v4_observation_complete": False, "online_v4_inference_ready": False,
                "readiness_scope": "native_observation", "compatible_inference_supported": True,
                "public_context_processing": "link_online",
                "public_messages_received": dict(self.public_message_counts),
                "tracked_hint_lifecycle_consistent": self.public_context.complete,
                "compatibility_warning_counts": dict(self.compatibility_warning_counts),
                "latest_compatibility_warnings": list(self.compatibility_warnings),
                "limitations": ["complete_transition_history_unavailable", "full_server_hint_coverage_unverified", "dynamic_attribute_coverage_unverified"]}

    # 以已完整解析的快照重建缓存与数量，不恢复报文未提供的卡片身份和实例提示
    def restore_field(self, restored, cards):
        if not self.started:
            self.warn_compatibility("reload_before_start")
            return False
        counts = {}
        for player, data in enumerate(restored["players"]):
            counts.update({(player, zone): count for zone, count in zip((1, 2, 16, 32, 64), data["counts"][:5])})
            counts[(player, 4)] = len(data["monsters"])
            counts[(player, 8)] = len(data["spells"])
        context = OnlinePublicContext((counts[(0, 1)], counts[(1, 1)]))
        # 快照没有撤销玩家级提示，也不保证旧卡片实例和剩余构筑可以继续追踪
        context.player_hints = deepcopy(self.public_context.player_hints)
        context.complete = False
        context.reversal_known = False
        context.remaining_composition_known = False
        self.counts, self.cards, self.public_context = counts, deepcopy(cards), context
        self.public_notifications.clear()
        self.invalidated_locations = ()
        self.last_known_draws = ()
        self.sequence += 1
        self.field_rebuilds += 1
        self.last_field_rebuild = {"sequence": self.sequence, "rule": restored["rule"],
            "extra_faceup_counts": [data["counts"][5] for data in restored["players"]]}
        for warning in restored["warnings"]:
            self.warn_compatibility(**warning)
        return True

    # 按真实接收者绑定提示与记牌可见性，不将单方消息升级为双方事实
    def public_snapshot(self):
        hints, cards, flags = self.public_context.snapshot()
        scope = 1 << self.perspective if self.perspective in (0, 1) else 0
        return (tuple(replace(hint, visibility=hint.visibility & scope) for hint in hints),
            tuple(replace(card, visibility=card.visibility & scope) for card in cards), flags)

    # 由明确的位置变化决定缓存失效范围，连续区域同时失效其槽位编号
    def _event_invalidations(self, kind, payload):
        if kind == 35:
            return tuple((actor, zone, None) for actor in (0, 1) for zone in ZONES)
        raw_locations = []
        if kind == 50:
            first, second = struct.unpack_from("<II", payload, 4)
            raw_locations.extend((first, second))
        elif kind == 55:
            _, first, _, second = struct.unpack("<IIII", payload)
            raw_locations.extend((first, second))
        elif kind == 53:
            actor, zone, sequence = payload[4:7]
            raw_locations.append(LocationInfo.encode(actor, zone, sequence, 0))
        elif kind in (32, 33, 39):
            return ((payload[0], {32: 1, 33: 2, 39: 64}[kind], None),)
        elif kind == 36:
            return tuple((actor, payload[0], None) for actor in (0, 1))
        locations = []
        for raw in raw_locations:
            actor, zone, sequence, _ = LocationInfo.decode(raw)
            zone &= ~Zone.OVERLAY
            if actor in (0, 1) and zone in ZONES:
                locations.append((actor, zone, sequence if zone in (4, 8) else None))
        return tuple(locations)

    # 判断物理位置是否已受本次实例变化影响
    def location_invalidated(self, location):
        return any(location[:2] == item[:2] and (item[2] is None or location[2] == item[2])
            for item in self.invalidated_locations)

    # 解析 Link 实际收到的公开提示与位置消息，不经过 Core 宿主过滤器
    def _observe_public_event(self, kind, payload, hand_counts):
        context = self.public_context
        if kind == 160:
            raw, hint_kind, value = struct.unpack("<IBI", payload)
            if card_hint_has_host(raw, hint_kind, value):
                context.card_hint(raw, hint_kind, value, self.turn)
        elif kind == 165:
            player, hint_kind, value = struct.unpack("<BBI", payload)
            context.player_hint(player, hint_kind, value, self.turn)
        elif kind == 120:
            raw, code = struct.unpack("<II", payload)
            self.public_notifications.append({"type": kind, "location_raw": raw,
                "code": code & 0x7FFFFFFF, "turn": self.turn, "sequence": self.sequence + 1,
                "visibility": 1 << self.perspective})
        elif kind == 40:
            player, = struct.unpack("<B", payload)
            if player not in (0, 1):
                raise ValueError("回合玩家编号无效")
            self.turn += 1
            self.turn_player = player
            self.turn_counts[player] += 1
            if self.starting_player is None:
                self.starting_player = player
        elif kind == 56:
            self.disabled_field_mask, = struct.unpack("<I", payload)
        elif kind in (30, 31, 42):
            confirmation = parse_online_confirmation(kind, payload)
            if kind == 31:
                size = confirmation["header_size"]
                if self.confirmation_header_size != size:
                    self.warn_compatibility("confirmation_layout_selected", header_size=size)
                self.confirmation_header_size = size
            for card in confirmation["cards"]:
                if card["location"] == Zone.DECK:
                    context.set_deck_card(card["controller"], card["sequence"], card["code"])
                code = card["code"] & 0x7FFFFFFF
                if code:
                    # 展示可发生在衍生物尚未进场时，只登记已见身份而不制造场面实体
                    self.public_notifications.append({"type": kind, "code": code, "turn": self.turn,
                        "sequence": self.sequence + 1, "visibility": 1 << self.perspective})
        elif kind in (32, 33, 39):
            player = payload[0]
            if player not in (0, 1):
                raise ValueError("洗牌玩家编号无效")
            zone = {32: Zone.DECK, 33: Zone.HAND, 39: Zone.EXTRA}[kind]
            if zone == Zone.DECK:
                context.deck_cards[player].clear()
            context.shuffle_hints(player, zone, descriptions_reemitted=kind == 33)
        elif kind == 36:
            if len(payload) < 2 or len(payload) != 2 + payload[1] * 8:
                raise ValueError("盖卡重排消息长度无效")
            values = struct.unpack("<" + "I" * (2 * payload[1]), payload[2:])
            context.shuffle_set_hints(values[:payload[1]], values[payload[1]:])
        elif kind == 37:
            if payload:
                raise ValueError("反转卡组消息包含多余字节")
            context.reverse_decks()
        elif kind == 38:
            context.deck_top(*struct.unpack("<BBI", payload))
        elif kind == 50:
            code, old_raw, new_raw, _ = struct.unpack("<IIII", payload)
            old_player, old_zone, _, old_pos = LocationInfo.decode(old_raw)
            _, new_zone, _, new_pos = LocationInfo.decode(new_raw)
            public = old_zone == Zone.GRAVE or new_zone == Zone.GRAVE or (
                old_zone in (4, 8, 32, 64) and old_pos & 5) or (new_zone in (4, 8, 32, 64) and new_pos & 5)
            context.move_hints(old_raw, new_raw)
            context.move_deck_card(old_raw, new_raw, code & 0x7FFFFFFF, bool(public))
        elif kind == 55:
            _, first, _, second = struct.unpack("<IIII", payload)
            first, second = location_key(first), location_key(second)
            context.remap_hints({first: second, second: first})
        elif kind == 90:
            player, count = payload[:2]
            if player not in (0, 1) or len(payload) != 2 + 4 * count:
                raise ValueError("抽卡消息布局无效")
            self.last_known_draws = tuple(card.code for card in context.draw(player, count, hand_counts[player]) if card is not None)
        elif kind == 35:
            # 尚未适配的墓地卡组交换先清除旧实例，Type162 由独立快照入口原子恢复
            self.public_context = OnlinePublicContext()
            # 区域重建本身不证明玩家级限制已撤销
            self.public_context.player_hints = [dict(hints) for hints in context.player_hints]
            self.public_context.complete = False
            self.public_context.reversal_known = False
        if kind in self.public_message_counts:
            self.public_message_counts[kind] += 1

    # 用真实公开移动和抽卡维护卡组数量且不恢复隐藏顺序
    def observe_event(self, kind, payload, *, hand_counts=(0, 0)):
        if not self.started:
            return
        self.last_known_draws = ()
        self._observe_public_event(kind, payload, hand_counts)
        self.invalidated_locations = self._event_invalidations(kind, payload)
        deltas = []
        if kind == 50 and len(payload) == 16:
            for offset, delta in ((4, -1), (8, 1)):
                actor, zone = payload[offset:offset + 2]
                deltas.append(((actor, zone), delta))
        elif kind == 90 and len(payload) >= 2:
            deltas.append(((payload[0], 1), -payload[1]))
        elif kind == 35:
            self.counts.clear()
        for key, delta in deltas:
            if key in self.counts:
                self.counts[key] = max(0, self.counts[key] + delta)
        self.cards = {key: value for key, value in self.cards.items() if not self.location_invalidated(key)}
        self.sequence += 1
