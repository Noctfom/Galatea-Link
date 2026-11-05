# V4 在线映射的独立报文夹具与只读 Core 快照参考

import ast
from copy import copy
import io
from pathlib import Path
import struct
import traceback
from types import MethodType, SimpleNamespace

from core import query_protocol as q
from core.gamestate import DuelState
from game_constants import LocationInfo


class FixtureCardReader:
    # 返回确定的静态元数据以隔离本机卡库版本
    def get_full_stats(self, code):
        return [33, 16, 16, 3, 0, 0, 0, 0, 0, 1800, (0, 0, 0, 0)]


# 按公开查询顺序构造完整字段，覆盖合法零阶级连接值和原始关系
def full_query(actor=0, zone=4, index=0, *, code=14558127, **updates):
    values = {"code": code, "position": LocationInfo.encode(actor, zone, index, 1),
        "current_code": 0, "current_type": 33, "current_level": 0, "current_rank": 4,
        "current_attr": 16, "current_race": 16, "current_atk": 2500, "current_def": 1000,
        "base_atk": 0, "base_def": 1800, "reason": 1, "reason_card": 0, "equip_target": 0,
        "targets": [], "overlays": [], "counters": [(5, 3)], "original_owner": actor,
        "status_mask": 9, "lscale": 1, "rscale": 2, "link_rating": 2, "link_marker": 0x12}
    values.update(updates)
    scalar_names = ("code", "position", "current_code", "current_type", "current_level", "current_rank",
        "current_attr", "current_race", "current_atk", "current_def", "base_atk", "base_def", "reason", "reason_card", "equip_target")
    body = struct.pack("<I", q.PUBLIC_CARD_QUERY_FLAGS)
    body += b"".join(struct.pack("<I", values[name] & 0xFFFFFFFF) for name in scalar_names)
    for name in ("targets", "overlays", "counters"):
        entries = values[name]
        if name == "counters":
            entries = [kind | count << 16 for kind, count in entries]
        body += struct.pack("<I", len(entries)) + b"".join(struct.pack("<I", value) for value in entries)
    body += struct.pack("<IIIIII", values["original_owner"], values["status_mask"], values["lscale"],
        values["rscale"], values["link_rating"], values["link_marker"])
    return struct.pack("<I", len(body) + 4) + body


# 构造包含普通召唤特殊召唤和阶段按钮的真实 Idle 字节布局
def idle_prompt(player=0):
    payload = bytes([player, 1]) + struct.pack("<IBBB", 14558127, player, 4, 0)
    payload += bytes([1]) + struct.pack("<IBBB", 89631139, player, 64, 0)
    payload += bytes([0, 0, 0, 0, 1, 1, 1])
    return payload


# 建立完整查询来源的测试快照，额外区库存与模型场面实体分开
def make_state(player=0):
    state = DuelState(model_protocol_version=4)
    state.effect_slot_resolver = lambda *_: None
    state.set_own_deck(player, [14558127, 89631139], [89631139])
    state.p0_deck, state.p1_deck = ([14558127, 89631139], []) if player == 0 else ([], [14558127, 89631139])
    state.p0_extra, state.p1_extra = ([89631139], []) if player == 0 else ([], [89631139])
    state.update(4, struct.pack("<BBIIHHHH", player, 5, 8000, 7000, 2, int(player == 0), 2, int(player == 1)))
    state.update(40, bytes([player]))
    rows = [(player, 4, 0, full_query(player, 4, 0)),
        (player, 4, 1, full_query(player, 4, 1, equip_target=LocationInfo.encode(player, 4, 0, 1),
            targets=[LocationInfo.encode(player, 4, 0, 1)], reason_card=LocationInfo.encode(player, 4, 0, 1))),
        (player, 64, 0, full_query(player, 64, 0, code=89631139))]
    for actor, zone, index, row in rows:
        state.update(7, bytes([actor, zone, index]) + row)
    state.update(11, idle_prompt(player))
    return state, rows


# 在内存中调用 Core 原始解析器和快照方法，不导入 Core 模块或写入 Core
def readonly_core_reference(root, state, rows, player=0, *, msg_type=11, payload=None, prepare_actions=None):
    from model_protocols.v4 import data_types as d
    from model_protocols.v4 import game_constants as constants
    from model_protocols.v4.event_history import validate_transition_history
    source = (Path(root) / "gamestate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DuelState")
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in {"get_snapshot", "_parse_valid_actions"}]
    assigns = [node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id in {"_IDLE_COMMAND_OPERATIONS", "_IDLE_COMMAND_SUMMON_METHODS", "_IDLE_COMMAND_LABELS"} for target in node.targets)]
    namespace = {**vars(d), **vars(constants), "copy": copy, "struct": struct, "io": io, "traceback": traceback,
        "card_db": FixtureCardReader(), "validate_transition_history": validate_transition_history}
    if msg_type == 16:
        # 在内存加载参考连锁解析函数，保持不导入 Core 或写入模块缓存
        protocol = ast.parse((Path(root) / "core_message_protocol.py").read_text(encoding="utf-8"))
        names = {"CoreMessageProtocolError", "_read_exact", "read_chain_prompt", "parse_chain_prompt"}
        nodes = [node for node in protocol.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<readonly Core chain>", "exec"), namespace)
    exec(compile(ast.Module(body=[*assigns, *methods], type_ignores=[]), "<readonly Core snapshot>", "exec"), namespace)
    projection = state.online_projection
    brain = SimpleNamespace(turn=state.turn, phase=state.phase, my_lp=state.my_lp, op_lp=state.op_lp,
        active_player=player, decision_player=player, turn_player=projection.turn_player,
        starting_player=projection.starting_player, player_turn_counts=list(projection.turn_counts), disabled_field_mask=0,
        p0_deck=list(state.p0_deck), p1_deck=list(state.p1_deck), p0_extra=list(state.p0_extra), p1_extra=list(state.p1_extra),
        p0_initial_deck=list(state._initial_decks[0][0] or ()), p1_initial_deck=list(state._initial_decks[1][0] or ()),
        p0_initial_extra=list(state._initial_decks[0][1] or ()), p1_initial_extra=list(state._initial_decks[1][1] or ()),
        field_map={0: {}, 1: {}}, _reload_counts={}, chain_stack=[], history_stack=[], known_hand_codes={0: [], 1: []},
        current_valid_actions=[], public_observation=projection.public_context,
        transition_recorder=SimpleNamespace(has_pending_boundary=False, snapshot=lambda: []))
    for actor, zone, index, raw in rows:
        if zone == 64:
            continue
        info = q.parse_legacy_card_query(raw)
        info["pos"] = info["position"]
        brain.field_map[actor].setdefault(zone, {})[index] = info
    brain._parse_valid_actions = MethodType(namespace["_parse_valid_actions"], brain)
    brain._parse_valid_actions(msg_type, io.BytesIO(idle_prompt(player) if payload is None else payload))
    if prepare_actions is not None:
        brain.current_valid_actions = prepare_actions(brain, list(brain.current_valid_actions))
    return namespace["get_snapshot"](brain)
