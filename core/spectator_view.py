# 观众公开快照投影，清除双方私有构筑和隐藏身份并保留已公告的公开事实

from copy import deepcopy
from dataclasses import replace

from data_types import CardEntity
from game_constants import LocationInfo, Zone


# 观众不因参照玩家改变而获得该玩家的手牌或盖放身份
def spectator_card_visible(card, *, grave_blocked=False):
    return card.location == Zone.GRAVE and not grave_blocked or bool(
        card.location in (Zone.MZONE, Zone.SZONE, Zone.REMOVED, Zone.EXTRA) and card.position & 5)


# 为公开观察与状态评测生成独立快照，保留原始物理座位而不认领任一决斗者
def public_spectator_snapshot(source):
    snapshot = deepcopy(source)
    coverage = dict(getattr(snapshot, 'online_coverage', {}))
    if coverage.get('visibility') != 'spectator_public':
        raise ValueError('公开观战投影只能处理实际观众接收的快照')
    flags = list(getattr(snapshot, 'public_state_flags', (False,) * 6))
    grave_blocked = len(flags) >= 5 and any(flags[3:5])
    visible_positions = set()
    for index, card in enumerate(snapshot.entities):
        if spectator_card_visible(card, grave_blocked=grave_blocked):
            visible_positions.add((card.owner, card.location, card.sequence))
            continue
        hidden = CardEntity(code=0, owner=card.owner, location=card.location, sequence=card.sequence,
            position=card.position, current_atk=0, current_def=0, type_mask=0, race=0,
            attribute=0, level=0, base_atk=0, base_def=0, overlay_count=card.overlay_count)
        hidden.query_known_fields = ()
        snapshot.entities[index] = hidden
    snapshot.valid_actions = []
    snapshot.global_data.decision_player = -1
    snapshot.action_prompt_type = None
    snapshot.selection_context = None
    snapshot.known_hand_codes = {0: [], 1: []}
    snapshot.initial_deck_known = {0: False, 1: False}
    for player in (0, 1):
        for section in ('deck', 'extra', 'initial_deck', 'initial_extra'):
            setattr(snapshot, f'p{player}_{section}_codes', [])
    # 当前构筑知识未知，不能把空画像声称为已知空卡组
    if len(flags) >= 6:
        flags[5] = False
    snapshot.public_state_flags = tuple(flags)
    hints = []
    for hint in getattr(snapshot, 'public_hints', ()):
        if hint.location >= 0 and LocationInfo.decode(hint.location)[:3] not in visible_positions:
            continue
        if hint.visibility:
            hints.append(replace(hint, visibility=3))
    snapshot.public_hints = tuple(hints)
    snapshot.known_deck_cards = tuple(card for card in getattr(snapshot, 'known_deck_cards', ()) if card.visibility == 3)
    # 私有选择提示不进入观众数据，实际收到的公告保留原投递字段
    facts, seen = [], set()
    for item in getattr(snapshot, 'prompt_context', ()):
        if item.get('subtype') not in (4, 6, 7, 8, 9, 10, 11):
            continue
        key = tuple(item.get(name, 0) for name in ('kind', 'subtype', 'wire_player', 'value', 'chain', 'prompt_type', 'code'))
        if key not in seen:
            seen.add(key)
            facts.append({**item, 'visibility_mask': 3})
    snapshot.prompt_context = tuple(facts)
    targets = list(getattr(snapshot, 'recent_chain_targets', ()))
    for chain in snapshot.chain_stack:
        targets.extend(chain.get('targets', ()))
    progress = getattr(snapshot, 'public_chain_progress', {})
    for chain in (*progress.get('current', ()), *progress.get('recent', ())):
        targets.extend(chain.get('targets', ()))
    for target in targets:
        if target.get('visibility_mask') != 3:
            target['code'], target['visibility_mask'] = 0, 0
    snapshot.observed_transitions = ()
    coverage['perspective'] = None
    snapshot.online_coverage = coverage
    return snapshot
