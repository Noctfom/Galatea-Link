# 在线复杂选择合同，独立校验网络载荷与响应且不使用本地核心的填充缓冲区

import io
import struct
from functools import lru_cache

from core.response_codec import encode_int_response


CANCEL_RESPONSE = b"\xff" * 4
SELECTION_MSGS = frozenset({15, 20, 21, 22, 23, 25, 26})
DEFAULT_SEARCH_BUDGET = 100_000


class SelectionProtocolError(ValueError):
    pass


class SelectionSearchLimit(RuntimeError):
    pass


# 读取指定结构并明确拒绝截断，字段错误不靠补零继续解析
def read_fields(stream, fmt):
    size = struct.calcsize(fmt)
    raw = stream.read(size)
    if len(raw) != size:
        raise SelectionProtocolError("复杂选择载荷截断")
    return struct.unpack(fmt, raw)


# 按当前在线布局解析复杂选择，保留候选顺序及协议独有的数值
def parse_selection_prompt(kind, payload):
    if kind not in SELECTION_MSGS:
        raise SelectionProtocolError("未知复杂选择类型")
    stream = io.BytesIO(bytes(payload))
    prompt = {"kind": kind, "cancelable": False, "finishable": False, "mandatory": []}
    if kind in (15, 20):
        player, cancel, minimum, maximum, count = read_fields(stream, "<BBBBB")
        prompt.update(player=player, cancelable=bool(cancel), minimum=minimum, maximum=maximum)
        rows = []
        for index in range(count):
            code, controller, zone, sequence, extra = read_fields(stream, "<IBBBB")
            rows.append({"index": index, "code": code, "controller": controller, "location": zone,
                "sequence": sequence, "position": extra if kind == 15 else 0,
                "release_value": extra if kind == 20 else 0})
    elif kind == 23:
        mode, player, target, minimum, maximum, count = read_fields(stream, "<BBIBBB")
        if mode not in (0, 1):
            raise SelectionProtocolError("未知求和模式")
        prompt.update(player=player, mode=mode, target=target, minimum=minimum, maximum=maximum)
        # 必选卡和可选卡共享数值结构，但响应编号只针对可选列表
        def read_sum_rows(size):
            result = []
            for index in range(size):
                code, controller, zone, sequence, value = read_fields(stream, "<IBBBI")
                result.append({"index": index, "code": code, "controller": controller, "location": zone,
                    "sequence": sequence, "position": 0, "sum_value": value})
            return result
        prompt["mandatory"] = read_sum_rows(count)
        rows = read_sum_rows(read_fields(stream, "<B")[0])
    elif kind == 22:
        player, counter_type, quantity, count = read_fields(stream, "<BHHB")
        prompt.update(player=player, counter_type=counter_type, quantity=quantity)
        rows = []
        for index in range(count):
            code, controller, zone, sequence, available = read_fields(stream, "<IBBBH")
            rows.append({"index": index, "code": code, "controller": controller, "location": zone,
                "sequence": sequence, "position": 0, "available": available})
    elif kind in (21, 25):
        player, count = read_fields(stream, "<BB")
        prompt["player"] = player
        rows = []
        for index in range(count):
            code, controller, zone, sequence = read_fields(stream, "<IBBB")
            rows.append({"index": index, "code": code, "controller": controller, "location": zone,
                "sequence": sequence, "position": 0})
    else:
        player, finish, cancel, minimum, maximum, count = read_fields(stream, "<BBBBBB")
        prompt.update(player=player, finishable=bool(finish), cancelable=bool(cancel), minimum=minimum, maximum=maximum)
        # Type26 的位置字段按四字节整体传回，不沿用其他选择的额外值含义
        def read_step_rows(size):
            return [dict(zip(("code", "location_info"), read_fields(stream, "<II"))) for _ in range(size)]
        prompt["selectable"] = read_step_rows(count)
        prompt["selected"] = read_step_rows(read_fields(stream, "<B")[0])
        if len(prompt["selectable"]) + len(prompt["selected"]) > 256:
            raise SelectionProtocolError("逐步选撤候选超过八位索引容量")
        rows = []
    prompt["candidates"] = rows
    if prompt["player"] not in (0, 1) or stream.read(1):
        raise SelectionProtocolError("复杂选择玩家无效或含多余字节")
    return prompt


# 将双数值素材展开为协议允许的候选数值，不选择其一冒充实际贡献
def sum_options(value):
    low, high = value & 0xFFFF, value >> 16 & 0xFFFF
    return (low, high) if high and high != low else (low,)


# 共用预算限制穷举状态数，避免无解组合长期占用后台线程
def spend_budget(budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise SelectionSearchLimit("复杂选择搜索达到本次预算")


# 复现原生精确和及最少素材边界，缓存仅作用于当前候选组合
def matches_sum(values, target, mode, budget):
    if mode:
        if not values:
            return target <= 0
        minimums = [min(sum_options(value)) for value in values]
        maximums = [max(sum_options(value)) for value in values]
        return sum(maximums) >= target and sum(minimums) - min(minimums) < target
    if not values or target > sum(max(sum_options(value)) for value in values):
        return False
    # 精确和必须与 Core 的分支顺序及零值处理保持一致
    @lru_cache(maxsize=8192)
    def check(index, remaining, previous_minimum):
        spend_budget(budget)
        if remaining == 0 or index == len(values):
            return False
        choices = sum_options(values[index])
        if index == len(values) - 1:
            return any(remaining == value and remaining + previous_minimum > value for value in choices)
        return any(remaining > value and check(index + 1, remaining - value, min(value, previous_minimum))
            for value in choices)
    return check(0, target, 0xFFFF)


# 校验网络响应完整长度与实际约束，不追加本地 Core 所需的固定长度填充
def validate_selection_response(kind, payload, response, *, prompt=None, budget=None):
    prompt = prompt or parse_selection_prompt(kind, payload)
    budget = [DEFAULT_SEARCH_BUDGET] if budget is None else budget
    if prompt["kind"] != kind:
        raise SelectionProtocolError("选择类型与提示不一致")
    raw = encode_int_response(-1 if response is None else response) if response is None or isinstance(response, int) else bytes(response)
    if kind == 22:
        rows = prompt["candidates"]
        if len(raw) != 2 * len(rows):
            raise SelectionProtocolError("指示物响应长度不匹配")
        counts = struct.unpack("<" + "H" * len(rows), raw)
        if sum(counts) != prompt["quantity"] or any(value > row["available"] for value, row in zip(counts, rows)):
            raise SelectionProtocolError("指示物分配超出可用量或总量不符")
        return raw
    if raw == CANCEL_RESPONSE or kind in (21, 25) and raw == b"\xff":
        if kind == 23 or kind not in (21, 25) and not (prompt["cancelable"] or prompt["finishable"]):
            raise SelectionProtocolError("当前提示不允许取消或完成")
        return raw
    if kind in (21, 25):
        if len(raw) != len(prompt["candidates"]) or set(raw) != set(range(len(raw))):
            raise SelectionProtocolError("排序响应必须是完整排列")
        return raw
    if not raw or len(raw) != raw[0] + 1:
        raise SelectionProtocolError("选择响应计数与长度不符")
    count, indices = raw[0], list(raw[1:])
    if kind == 26:
        if count != 1:
            raise SelectionProtocolError("逐步选撤必须回复一个索引")
        candidate_count = len(prompt["selectable"]) + len(prompt["selected"])
    else:
        candidate_count = len(prompt["candidates"])
    if kind == 23:
        mandatory = prompt["mandatory"]
        if count < len(mandatory):
            raise SelectionProtocolError("求和响应缺少必选占位")
        indices = indices[len(mandatory):]
        if not prompt["mode"] and not prompt["minimum"] <= len(indices) <= prompt["maximum"]:
            raise SelectionProtocolError("精确和的可选素材张数不符")
    if len(indices) != len(set(indices)) or any(index >= candidate_count for index in indices):
        raise SelectionProtocolError("选择索引越界或重复")
    if kind == 15 and not prompt["minimum"] <= count <= prompt["maximum"]:
        raise SelectionProtocolError("选卡数量不满足范围")
    if kind == 20 and (count > prompt["maximum"] or
            sum(prompt["candidates"][index]["release_value"] for index in indices) < prompt["minimum"]):
        raise SelectionProtocolError("祭品张数或解放值不满足要求")
    if kind == 23:
        values = [row["sum_value"] for row in prompt["mandatory"]]
        values += [prompt["candidates"][index]["sum_value"] for index in indices]
        if not matches_sum(tuple(values), prompt["target"], prompt["mode"], budget):
            raise SelectionProtocolError("素材数值不满足求和边界")
    return raw


# 只公开提示约束，不在共享上下文重复卡密或原始响应
def selection_context(prompt):
    kind = prompt["kind"]
    context = {"source": "server_prompt", "candidate_count": len(prompt["candidates"])}
    if kind in (15, 26):
        context.update(minimum=prompt["minimum"], maximum=prompt["maximum"],
            cancelable=prompt["cancelable"], finishable=prompt["finishable"])
        if kind == 26:
            context.update(candidate_count=len(prompt["selectable"]), selected_count=len(prompt["selected"]))
    elif kind == 20:
        context.update(minimum_release_value=prompt["minimum"], maximum_cards=prompt["maximum"], cancelable=prompt["cancelable"])
    elif kind == 22:
        context.update(counter_type=prompt["counter_type"], required_counters=prompt["quantity"])
    elif kind == 23:
        context.update(mode="at_least" if prompt["mode"] else "exact", target=prompt["target"],
            mandatory_count=len(prompt["mandatory"]), optional_count_limits_apply=not bool(prompt["mode"]))
        if not prompt["mode"]:
            context.update(minimum_optional=prompt["minimum"], maximum_optional=prompt["maximum"])
    else:
        context.update(order_length=len(prompt["candidates"]), order_semantics="first_to_last",
            sort_kind="chain" if kind == 21 else "card", default_order_available=kind == 21)
        if kind == 21:
            context["effect_descriptions_available"] = False
    return context
