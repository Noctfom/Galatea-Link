# 有界复杂选择候选生成，保留实例差异并让完整候选通过实际响应校验

import itertools
import struct

from core.selection_protocol import (CANCEL_RESPONSE, DEFAULT_SEARCH_BUDGET, SelectionProtocolError,
    SelectionSearchLimit, parse_selection_prompt, spend_budget, validate_selection_response)
from game_constants import LocationInfo


# 把可见实例事实变为稳定键，采集序号不影响卡片等价性
def freeze_facts(value):
    if isinstance(value, dict):
        return tuple((key, freeze_facts(item)) for key, item in sorted(value.items())
            if not key.startswith("query_") and key not in ("online_query_fields", "relation_known_fields"))
    if isinstance(value, (tuple, list)):
        return tuple(freeze_facts(item) for item in value)
    return value


# 场上墓地除外均保留独立实例，隐藏区只合并实际已知状态及提示值相同的副本
def equivalence_key(card, brain):
    index, zone = card["index"], card["location"]
    player = card["controller"]
    info = getattr(brain, "field_map", {}).get(player, {}).get(zone, {}).get(card["sequence"], {})
    if zone not in (1, 2, 64) or not card["code"] or not info:
        return ("instance", index)
    return ("known_copy", player, zone, card["code"], card["position"],
        card.get("release_value"), card.get("sum_value"), freeze_facts(info))


# 将提示卡片转换为完整位置，祭品的末字节不解释成表示形式
def location_of(card):
    return card.get("location_info", LocationInfo.encode(card.get("controller", 0), card.get("location", 0),
        card.get("sequence", 0), card.get("position", 0)))


# 按已知等价组枚举指定张数，搜索预算同时约束无解分支
def grouped_combinations(groups, count, budget):
    # 只生成一个规范副本组合，保留不同组之间的全部组合关系
    def walk(index, remaining, path):
        spend_budget(budget)
        if remaining == 0:
            yield tuple(path)
            return
        if index == len(groups) or remaining > sum(len(group) for group in groups[index:]):
            return
        group = groups[index]
        for take in range(min(len(group), remaining), -1, -1):
            yield from walk(index + 1, remaining - take, path + group[:take])
    yield from walk(0, count, [])


# 生成完整响应并统一过滤非法组合，达到预算时只保留已验收候选
def build_selection_options(kind, payload, brain=None, *, limit=5000, pref_weights=None, search_budget=DEFAULT_SEARCH_BUDGET):
    if type(limit) is not int or limit < 1 or type(search_budget) is not int or search_budget < 1:
        raise ValueError("复杂选择候选数量及搜索预算必须为正整数")
    prompt = parse_selection_prompt(kind, payload)
    weights, budget = pref_weights or {}, [search_budget]
    options = []
    # 完整响应通过校验后才进入结果，不允许取消项绕开原提示的约束
    def append(raw, cards=(), values=()):
        try:
            validate_selection_response(kind, payload, raw, prompt=prompt, budget=budget)
        except SelectionProtocolError:
            return
        option = {"bytes": bytes(raw), "locs": [location_of(card) for card in cards],
            "codes": [card["code"] for card in cards], "values": list(values)}
        if kind in (21, 25):
            option["indices"] = [card["index"] for card in cards]
        options.append(option)
    try:
        if kind == 26:
            candidates = prompt["selectable"] + prompt["selected"]
            for index in range(min(256, len(candidates))):
                spend_budget(budget)
                append(bytes([1, index]))
                if len(options) >= limit:
                    break
            if len(options) < limit and (prompt["finishable"] or prompt["cancelable"]):
                append(CANCEL_RESPONSE)
        elif kind in (21, 25):
            # 自动连锁排序由服务器决定顺序，不表示取消发动这些效果
            if kind == 21:
                append(CANCEL_RESPONSE)
            cards = prompt["candidates"]
            for order in itertools.permutations(cards):
                if len(options) >= limit:
                    break
                spend_budget(budget)
                # 网络按原候选索引保存名次，展示目标列表按结果先后排列
                ranks = bytearray(len(cards))
                for rank, card in enumerate(order):
                    ranks[card["index"]] = rank
                append(bytes(ranks), order, range(1, len(order) + 1))
                if len(options) >= limit:
                    break
        elif kind == 22:
            cards = prompt["candidates"]
            suffix = [0] * (len(cards) + 1)
            for index in range(len(cards) - 1, -1, -1):
                suffix[index] = suffix[index + 1] + cards[index]["available"]
            # 每个候选对应一个 uint16，零分配仍保留在网络向量中
            def distribute(index, remaining, counts):
                spend_budget(budget)
                if len(options) >= limit or remaining > suffix[index]:
                    return
                if index == len(cards):
                    selected = [(card, count) for card, count in zip(cards, counts) if count]
                    append(struct.pack("<" + "H" * len(counts), *counts),
                        [card for card, _ in selected], [count for _, count in selected])
                    return
                lower = max(0, remaining - suffix[index + 1])
                for take in range(min(cards[index]["available"], remaining), lower - 1, -1):
                    distribute(index + 1, remaining - take, [*counts, take])
                    if len(options) >= limit:
                        return
            distribute(0, prompt["quantity"], [])
        else:
            cards = sorted(prompt["candidates"], key=lambda card: weights.get(card["code"], 0), reverse=True)
            grouped = {}
            for card in cards:
                grouped.setdefault(equivalence_key(card, brain), []).append(card)
            groups = list(grouped.values())
            mandatory = prompt["mandatory"]
            if kind == 15 or kind == 23 and not prompt["mode"]:
                lower, upper = prompt["minimum"], min(prompt["maximum"], len(cards))
            else:
                lower = 0
                upper = min(prompt["maximum"], len(cards)) if kind == 20 else len(cards)
            upper = min(upper, 255 - len(mandatory))
            if prompt["cancelable"]:
                append(CANCEL_RESPONSE)
            for count in range(lower, upper + 1):
                if len(options) >= limit:
                    break
                for combo in grouped_combinations(groups, count, budget):
                    combo = tuple(sorted(combo, key=lambda card: card["index"]))
                    chosen = [*mandatory, *combo]
                    raw = bytes([len(chosen)]) + bytes(len(mandatory)) + bytes(card["index"] for card in combo)
                    values = [card.get("sum_value", card.get("release_value", 0)) for card in chosen]
                    append(raw, chosen, values)
                    if len(options) >= limit:
                        break
    except SelectionSearchLimit:
        if not options:
            raise
        print(f"[选择候选] Type {kind} 搜索达到预算，保留 {len(options)} 个已验证候选")
    if len(options) >= limit:
        print(f"[选择候选] Type {kind} 达到数量上限 {limit}，返回已验证候选")
    return options
