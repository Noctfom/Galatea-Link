# 当前询问与逐链上下文状态，复用已审核的 Core 语义并独立适配真实网络可见性，固定复用 Core V4 协议且不依赖外部 Core 目录

from copy import deepcopy
import struct

from data_types import ActionOperation
from game_constants import LocationInfo, Zone
from core.public_context import location_key

_DECISION_MESSAGES = {10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26, 140, 141, 142, 143}
_SELECT_HINT_PROMPTS = {15, 18, 20, 23, 24, 26}


# 使用真实网络玩家字段，求和提示的首字节单独存放模式
# 复用 Core V4 的 decision_player_from_message 协议逻辑
def decision_player_from_message(kind, payload):
    return payload[1 if kind == 23 else 0]


# 保留提示的原投递字段，未知子类型不构造观测事实
# 复用 Core V4 的 hint_visibility_mask 协议逻辑
def hint_visibility_mask(subtype, wire_player):
    if subtype in (1, 2, 3, 5):
        return 1 << wire_player
    if subtype in (4, 6, 7, 8, 9):
        return 1 << (1 - wire_player)
    return 3 if subtype in (10, 11) else 0


class CurrentContextState:
    # 初始化单局上下文与身份失效标记，场面重建不继承旧选择用途
    def _reset_current_context(self):
        self.resolving_chain_index = 0
        self._chain_target_dirty_zones = set()
        self._chain_target_dirty_sequence = {}
        self._reset_prompt_context()

    # 解析线上提示的已知窄宽布局，语义未知只记录警告不影响对局
    def _parse_online_hint(self, payload):
        if len(payload) not in (6, 10):
            self.online_projection.warn_compatibility('current_hint_layout', payload_bytes=len(payload))
            return None
        subtype, player = payload[:2]
        if player not in (0, 1) or not hint_visibility_mask(subtype, player):
            self.online_projection.warn_compatibility('current_hint_semantics', subtype=subtype, wire_player=player)
            return None
        value = int.from_bytes(payload[2:], 'little')
        if value > 0xFFFFFFFF:
            self.online_projection.warn_compatibility('current_hint_value_width', subtype=subtype, value_bytes=len(payload) - 2)
            return None
        return subtype, player, value

    # 只根据公开区域与正面表示判断身份是否已公开，不读取本地完整卡组
    def _is_public_event_location(self, raw):
        player, zone, sequence, position = LocationInfo.decode(raw)
        if zone & Zone.OVERLAY or zone == Zone.GRAVE:
            return True
        if zone not in (Zone.MZONE, Zone.SZONE, Zone.REMOVED, Zone.EXTRA):
            return False
        entry = self.field_map.get(player, {}).get(zone, {}).get(sequence)
        if not position and entry is not None:
            position = entry.get('pos', 0)
        return bool(position & 5)

    # 记下连续区域身份失效的时点，后续单卡查询可以分别恢复可信位置
    def _mark_chain_target_dirty_zone(self, player, zone):
        self._chain_target_dirty_zones.add((player, zone))
        self._chain_target_dirty_sequence[(player, zone)] = self.online_projection.sequence

    # 维护网络可证明的提示和实例变化，当前链号不因结算而重编号
    def _observe_current_context(self, kind, payload):
        self._update_prompt_context(kind, payload)
        if kind == 70:
            self.recent_chain_targets = []
        elif kind in (71, 72, 73, 75, 76):
            number, = struct.unpack('<B', payload)
            chain = self._find_chain_context(number)
            if kind == 72:
                self.resolving_chain_index = number
                for previous in self.chain_stack:
                    previous['solving'] = False
            if chain is not None:
                if kind == 71:
                    chain['confirmed'] = True
                elif kind == 72:
                    chain['solving'] = True
                elif kind == 73:
                    chain['solving'], chain['solved'] = False, True
                elif kind == 75:
                    chain['activation_negated'] = True
                else:
                    chain['effect_disabled'] = True
        elif kind == 74:
            self.resolving_chain_index = 0
        elif kind == 83:
            self._record_chain_targets(payload)
        elif kind in (32, 33, 34, 39):
            self._invalidate_chain_target_zone(payload[0], {32: Zone.DECK, 33: Zone.HAND, 34: Zone.DECK, 39: Zone.EXTRA}[kind])
        elif kind == 35:
            self._invalidate_chain_target_zone(payload[0], Zone.DECK)
            self._invalidate_chain_target_zone(payload[0], Zone.GRAVE)
        elif kind == 37:
            for player in (0, 1):
                self._invalidate_chain_target_zone(player, Zone.DECK)
        elif kind == 36:
            count = payload[1]
            positions = struct.unpack('<' + 'I' * (2 * count), payload[2:])
            self._remap_chain_target_locations({location_key(old): new for old, new in zip(positions[:count], positions[count:])})
            for raw in positions:
                if raw:
                    self._mark_chain_target_dirty_zone(*location_key(raw)[:2])
        elif kind == 50:
            code, old, new, _ = struct.unpack('<IIII', payload)
            self._move_chain_target_locations(old, new, code=code & 0x7FFFFFFF,
                public=self._is_public_event_location(old) or self._is_public_event_location(new))
        elif kind == 53:
            code, player, zone, sequence, _, position = struct.unpack('<IBBBBB', payload)
            for target in self._iter_chain_targets():
                if target.get('tracked') and location_key(target['current_location'])[:3] == (player, zone, sequence):
                    target['current_location'] = LocationInfo.encode(player, zone, sequence, position)
                    if position & 5 and code:
                        target['code'], target['visibility_mask'] = code & 0x7FFFFFFF, 3
        elif kind == 55:
            _, first, _, second = struct.unpack('<IIII', payload)
            self._remap_chain_target_locations({location_key(first): second, location_key(second): first})
            for raw in (first, second):
                self._mark_chain_target_dirty_zone(*location_key(raw)[:2])
        elif kind == 90 and payload[1]:
            self._invalidate_chain_target_zone(payload[0], Zone.DECK)
        self._sync_current_context_progress()

    # 保留原有公开连锁通知出口，向同一记录补充当前上下文事实
    def _sync_current_context_progress(self):
        for chain in self.chain_stack:
            progress = self.public_chain_progress.current.get(chain['ct'])
            if progress is not None:
                progress.update(deepcopy(chain))

    # 只用本次确实刷新的槽位校准对象，单卡刷新不证明整区已经重建
    def _refresh_chain_target_identities(self, player, zone, changes, *, full=False):
        if full:
            self._chain_target_dirty_zones.discard((player, zone))
        for target in self._iter_chain_targets():
            if not target.get('tracked'):
                continue
            owner, raw_zone, sequence, overlay = LocationInfo.decode(target['current_location'])
            if (owner, raw_zone & ~Zone.OVERLAY) != (player, zone) or not full and sequence not in changes:
                continue
            entry = self.field_map[player].get(zone, {}).get(sequence)
            if entry is None or raw_zone & Zone.OVERLAY and overlay >= len(entry.get('overlay_codes', entry.get('overlays', ()))):
                target['current_location'], target['tracked'] = -1, False
                continue
            visible = self._capture_chain_target(target['current_location'])
            if visible['code']:
                if target['code'] and target['code'] != visible['code']:
                    target['current_location'], target['tracked'] = -1, False
                else:
                    target['code'] = visible['code']
                    target['visibility_mask'] |= visible['visibility_mask']
        self._sync_current_context_progress()

    # 清空提示实例，重载缺少的选择用途不会沿用上一场景
    def _reset_prompt_context(self):
        """清空提示实例，重载缺少的选择用途不会沿用上一场景"""
        self.recent_chain_targets = []
        self._recent_chain_targets_presented = False
        self._select_hints = [None, None]
        self._hint_revision = 0
        self._prompt_kind = 0
        self._prompt_player = -1
        self._prompt_hint_revision = None
        self._prompt_response = None
        self._hint_notifications = {0: [], 1: []}

    # 在决策生成前更新用途，响应仅暂记消费，重试仍保留原提示
    def _update_prompt_context(self, msg_type, payload):
        """在决策生成前更新用途，响应仅暂记消费，重试仍保留原提示"""
        if msg_type in (5, 40, 41, 74):
            self._reset_prompt_context()
            return
        if msg_type == 1:
            self._prompt_response = None
            self._recent_chain_targets_presented = False
            return
        if msg_type == 2:
            parsed = self._parse_online_hint(payload)
            if parsed is None:
                return
            subtype, wire_player, value = parsed
            if subtype == 3:
                self._hint_revision += 1
                self._select_hints[wire_player] = (self._hint_revision, value)
                return
            chain = int(getattr(self, 'resolving_chain_index', 0))
            if not chain and self.chain_stack:
                chain = int(self.chain_stack[-1]['ct'])
            visibility = hint_visibility_mask(subtype, wire_player)
            for viewer in (0, 1):
                if not visibility & (1 << viewer):
                    continue
                record = dict(kind=2, subtype=subtype, wire_player=wire_player,
                              value=value, visibility_mask=1 << viewer, chain=chain,
                              prompt_type=0, code=value if subtype in (8, 10) else 0)
                notices = self._hint_notifications[viewer]
                notices.append(record)
                # 无链通知只保留最近16项，链内公告保留到链结束
                unlinked = [item for item in notices if item['chain'] == 0]
                if len(unlinked) > 16:
                    notices.remove(unlinked[0])
            return
        if msg_type not in _DECISION_MESSAGES:
            return
        player = decision_player_from_message(msg_type, payload)
        # 无链公告只属于接下来的决策窗口，Retry 仍使用同一窗口
        if self._recent_chain_targets_presented:
            self.recent_chain_targets = []
        self._recent_chain_targets_presented = bool(self.recent_chain_targets)
        previous = self._prompt_response
        if previous is not None:
            old_player, old_kind, revision, finished = previous
            continuing = old_kind == 26 and msg_type == 26 and player == old_player and not finished
            old_hint = self._select_hints[old_player]
            if not continuing and old_hint is not None and old_hint[0] == revision:
                self._select_hints[old_player] = None
            self._prompt_response = None
        self._prompt_kind, self._prompt_player = int(msg_type), player
        hint = self._select_hints[player]
        self._prompt_hint_revision = hint[0] if hint is not None and msg_type in _SELECT_HINT_PROMPTS else None

    # 成功发送不等于Core已接受，下一有效询问才最终消费原用途
    def _note_prompt_response(self, actor, msg_type, response, action):
        """成功发送不等于Core已接受，下一有效询问才最终消费原用途"""
        if msg_type not in _SELECT_HINT_PROMPTS or actor != self._prompt_player:
            return
        finished = getattr(action, 'operation_id', -1) in (ActionOperation.FINISH, ActionOperation.CANCEL)
        if isinstance(response, int):
            finished = finished or response == -1
        elif isinstance(response, (bytes, bytearray)):
            finished = finished or bytes(response[:4]) == b'\xff\xff\xff\xff'
        self._prompt_response = (actor, msg_type, self._prompt_hint_revision, finished)
        return self._prompt_response

    # 仅投影当前选择者可见的用途，公告保留原始子类型与投递角色
    def _snapshot_prompt_context(self):
        """仅投影当前选择者可见的用途，公告保留原始子类型与投递角色"""
        result = [dict(item) for notices in self._hint_notifications.values() for item in notices]
        player = self._prompt_player
        if player in (0, 1) and self._prompt_kind in _SELECT_HINT_PROMPTS and self.active_player == player and self.current_valid_actions:
            hint = self._select_hints[player]
            if hint is not None:
                result.append(dict(kind=2, subtype=3, wire_player=player, value=hint[1],
                                   visibility_mask=1 << player, chain=0, prompt_type=self._prompt_kind))
        return tuple(result)

    # 区分从发动起完整接收的链节与中途重载后未包含的上下文
    @staticmethod
    def _initial_chain_context(*, known=True):
        """区分从发动起完整接收的链节与中途重载后未包含的上下文"""
        return {
            'status_known': bool(known),
            'targets_known': bool(known),
            'confirmed': False,
            'solving': False,
            'solved': False,
            'activation_negated': False,
            'effect_disabled': False,
            'targets': [],
        }

    # 按 Core 原始链号查找，已经处理的节点仍保留到连锁结束
    def _find_chain_context(self, chain_index):
        """按 Core 原始链号查找，已经处理的节点仍保留到连锁结束"""
        return next((item for item in self.chain_stack if item['ct'] == chain_index), None)

    # 遍历活动链与最近公告的独立目标副本
    def _iter_chain_targets(self):
        """遍历活动链与最近公告的独立目标副本"""
        for item in self.chain_stack:
            yield from item.get('targets', ())
        yield from self.recent_chain_targets

    # 记录公告时的对象身份，可见性与后续位置分开保存
    def _capture_chain_target(self, raw_location):
        """记录公告时的对象身份，可见性与后续位置分开保存"""
        controller, zone, sequence, position = LocationInfo.decode(raw_location)
        code = 0
        entry = None
        if controller in (0, 1):
            entry = self.field_map[controller].get(zone & ~Zone.OVERLAY, {}).get(sequence)
            dirty = (controller, zone & ~Zone.OVERLAY)
            if dirty in self._chain_target_dirty_zones and entry is not None:
                query_sequence = entry.get('query_sequence')
                if type(query_sequence) is not int or query_sequence <= self._chain_target_dirty_sequence.get(dirty, -1):
                    entry = None
            if entry is not None:
                if zone & Zone.OVERLAY:
                    overlays = entry.get('overlay_codes', entry.get('overlays', ()))
                    if 0 <= position < len(overlays):
                        code = int(overlays[position]) & 0x7FFFFFFF
                else:
                    code = int(entry.get('code', 0)) & 0x7FFFFFFF
        # 超量素材为公开信息，未知实体不从完整卡组构成反推身份
        public = bool(zone & Zone.OVERLAY) or self._is_public_event_location(raw_location)
        own_known = controller in (0, 1) and zone in (Zone.HAND, Zone.MZONE, Zone.SZONE, Zone.REMOVED, Zone.EXTRA)
        visibility = 0b11 if public else ((1 << controller) if own_known else 0)
        if zone == Zone.GRAVE:
            for viewer in (0, 1):
                if self.online_projection.public_context.grave_blocked(viewer):
                    visibility &= ~(1 << viewer)
        return {
            'announcement_location': int(raw_location),
            'current_location': int(raw_location),
            'code': code if visibility else 0,
            'visibility_mask': visibility if code else 0,
            'tracked': controller in (0, 1) and zone != 0,
        }

    # 保存 MSG83 公开指示对象，不把 HintSelection 当作规则上的持久取对象关系
    def _record_chain_targets(self, payload):
        """保存 MSG83 公开指示对象，不把 HintSelection 当作规则上的持久取对象关系"""
        if not payload or len(payload) != 1 + 4 * payload[0]:
            raise ValueError('invalid public chain target payload')
        locations = struct.unpack('<' + 'I' * payload[0], payload[1:])
        announced = [self._capture_chain_target(raw) for raw in locations]
        chain = self._find_chain_context(self.resolving_chain_index)
        if chain is None and self.chain_stack and not self.resolving_chain_index:
            chain = self.chain_stack[-1]
        if chain is None:
            # HintSelection 对组内卡片逐条公告，必须合并同一窗口内的 MSG83
            if self._recent_chain_targets_presented:
                self.recent_chain_targets = []
                self._recent_chain_targets_presented = False
            targets = self.recent_chain_targets
        else:
            self.recent_chain_targets = []
            targets = chain.setdefault('targets', [])
        for target in announced:
            # 重复公告同一可追踪实例只保留一份，失联旧对象不能与新占位者合并
            if any(old.get('tracked') and location_key(old['current_location']) == location_key(target['current_location'])
                   for old in targets):
                continue
            targets.append(target)

    # 洗牌后保留曾公开的对象事实，停止断言它仍处在旧位置
    def _invalidate_chain_target_zone(self, player, zone):
        """洗牌后保留曾公开的对象事实，停止断言它仍处在旧位置"""
        self._mark_chain_target_dirty_zone(player, zone)
        for target in self._iter_chain_targets():
            if not target.get('tracked'):
                continue
            current = location_key(target['current_location'])
            if current[0] == player and current[1] == zone:
                target['current_location'] = -1
                target['tracked'] = False

    # 同时交换公开可追踪位置，空目标位置代表已经失联
    def _remap_chain_target_locations(self, mapping):
        """同时交换公开可追踪位置，空目标位置代表已经失联"""
        for target in self._iter_chain_targets():
            if not target.get('tracked'):
                continue
            raw = target['current_location']
            key = location_key(raw)
            for old, new_raw in mapping.items():
                material = key[:3] == (old[0], old[1] | Zone.OVERLAY, old[2]) and not old[1] & Zone.OVERLAY
                if key != old and not material:
                    continue
                if not new_raw:
                    target['current_location'] = -1
                    target['tracked'] = False
                elif material:
                    nc, nl, ns, _ = LocationInfo.decode(new_raw)
                    target['current_location'] = LocationInfo.encode(nc, nl | Zone.OVERLAY, ns, key[3])
                else:
                    target['current_location'] = int(new_raw)
                break

    # 按公开移动平移目标位置，不把新占位卡继承为曾经公告的对象
    def _move_chain_target_locations(self, old_raw, new_raw, *, code=0, public=False):
        """按公开移动平移目标位置，不把新占位卡继承为曾经公告的对象"""
        old, new = location_key(old_raw), location_key(new_raw)
        if old == new:
            return
        contiguous = (Zone.DECK, Zone.HAND, Zone.GRAVE, Zone.REMOVED, Zone.EXTRA)
        # 普通镜像在连续区移动后要等 query 才重排，期间不能从旧序号读身份
        for key in (old, new):
            if key[0] in (0, 1) and key[1] in contiguous:
                self._mark_chain_target_dirty_zone(*key[:2])
        for target in self._iter_chain_targets():
            if not target.get('tracked'):
                continue
            raw = target['current_location']
            controller, zone, sequence, position = LocationInfo.decode(raw)
            key = location_key(raw)
            if key == old:
                if public and code:
                    target['code'] = int(code) & 0x7FFFFFFF
                    target['visibility_mask'] = 0b11
                target['current_location'] = int(new_raw) if new[0] in (0, 1) and new[1] else -1
                target['tracked'] = target['current_location'] >= 0
                continue
            if not old[1] & Zone.OVERLAY and key[:3] == (old[0], old[1] | Zone.OVERLAY, old[2]):
                if new[0] in (0, 1) and new[1] in (Zone.MZONE, Zone.SZONE):
                    target['current_location'] = LocationInfo.encode(new[0], new[1] | Zone.OVERLAY, new[2], position)
                else:
                    target['current_location'], target['tracked'] = -1, False
                continue
            if old[1] & Zone.OVERLAY and key[:3] == old[:3] and position > old[3]:
                position -= 1
            elif old[1] in contiguous and key[:2] == old[:2] and sequence > old[2]:
                sequence -= 1
            if new[1] & Zone.OVERLAY and (controller, zone, sequence) == new[:3] and position >= new[3]:
                position += 1
            elif new[1] in contiguous and (controller, zone) == new[:2] and sequence >= new[2]:
                sequence += 1
            elif new[1] not in contiguous and key == new:
                target['current_location'], target['tracked'] = -1, False
                continue
            target['current_location'] = LocationInfo.encode(controller, zone, sequence, position)
