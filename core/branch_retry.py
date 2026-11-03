# 部署阶段防止选择与取消形成闭环，参考 Core 末端优先策略且不依赖 Torch

from dataclasses import fields, is_dataclass

from data_types import ActionOperation


LOOP_POLICY = "leaf_first_v1"


# 冻结实际游戏事实，排除查询序号等不会改变局面的采集元数据
def freeze_world(value):
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, dict):
        return tuple((key, freeze_world(item)) for key, item in sorted(value.items())
            if not key.startswith("query_") and key not in {"to_play", "decision_player", "relation_known_fields"})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_world(item) for item in value)
    if hasattr(value, "__dict__"):
        return freeze_world(vars(value))
    raise ValueError("闭环检测遇到未知快照字段类型")


# 显式响应不依赖宏动作池的临时序号，描述文本也不参与候选身份
def action_token(action):
    names = (field.name for field in fields(action)) if is_dataclass(action) else vars(action)
    explicit = bool(getattr(action, "decision_bytes", None)) or getattr(action, "decision_value", None) is not None
    return freeze_world({name: getattr(action, name) for name in names
        if name != "desc_str" and not (explicit and name == "index")})


class CoreBranchRetry:
    MAX_PATH = 256
    MAX_CYCLES = 1024

    # 每局独立维护分支，软避让不会覆盖服务器明确拒绝的动作
    def __init__(self, threshold=5):
        self.threshold = threshold
        self._epoch = 0
        self.reset()

    # 清空对局或配置变化前的分支证据并使旧异步令牌失效
    def reset(self):
        self._epoch += 1
        self._world = None
        self._paths = {}
        self._cycles = {}
        self._soft_bans = {}

    # 忽略行动者切换和响应历史步号，仅比较当前可见局面
    def world_key(self, snapshot):
        return tuple(freeze_world(item) for item in (snapshot.global_data, snapshot.entities,
            snapshot.chain_stack, snapshot.history_stack, getattr(snapshot, "public_hints", ()),
            getattr(snapshot, "known_deck_cards", ()), getattr(snapshot, "public_state_flags", ())))

    # 进入真实提示并检测已接受路径的闭合回退，服务器 Retry 不增加循环次数
    def enter(self, snapshot, msg_type, payload, *, hard_bans=(), engine_retry=False):
        world = self.world_key(snapshot)
        if world != self._world:
            self.reset()
            self._world = world
        player = snapshot.global_data.to_play
        path = self._paths.setdefault(player, [])
        base = (msg_type, player, bytes(payload))
        tokens = tuple(action_token(action) for action in snapshot.valid_actions)
        changes = []
        if engine_retry:
            if path and path[-1]["base"] == base:
                path.pop()
        else:
            position = next((index for index, node in enumerate(path) if node["base"] == base), None)
            if position is not None:
                returned = msg_type in (10, 11) or any(node["backtrack"] for node in path[position:])
                signature = (path[position]["scope"], path[position]["token"])
                if signature not in self._cycles and len(self._cycles) >= self.MAX_CYCLES:
                    self._cycles.clear()
                count = self._cycles.get(signature, 0) + 1 if returned else 0
                if returned:
                    self._cycles[signature] = count
                if count >= self.threshold:
                    self._cycles[signature] = 0
                    for depth in range(len(path) - 1, position - 1, -1):
                        node = path[depth]
                        banned = self._soft_bans.setdefault(node["scope"], set())
                        banned.add(node["token"])
                        changes.append({"message_type": node["base"][0], "choice_id": node["selected"],
                            "depth": depth - position + 1, "repeat_count": count})
                        if node["available"] - banned - node["hard"]:
                            break
                del path[position:]
        if len(path) >= self.MAX_PATH:
            path.clear()
        if len(self._soft_bans) > self.MAX_CYCLES:
            self._soft_bans.clear()
        scope = (base, tuple((node["base"], node["token"]) for node in path))
        hard = {tokens[index] for index in hard_bans if 0 <= index < len(tokens)}
        banned = self._soft_bans.get(scope, set())
        released = False
        if banned and not set(tokens) - banned - hard:
            self._soft_bans.pop(scope, None)
            banned, released = set(), True
        snapshot.core_soft_banned_choice_ids = tuple(index for index, token in enumerate(tokens) if token in banned)
        snapshot._core_branch_context = (self._epoch, player, base, scope, tokens, hard)
        return changes, released

    # 发送前暂存真实选择，使 drain 等待期间到达的下一提示仍能看到末端路径
    def stage(self, snapshot, selected, *, available=None):
        context = getattr(snapshot, "_core_branch_context", None)
        if context is None or context[0] != self._epoch:
            return None
        epoch, player, base, scope, tokens, hard = context
        if not 0 <= selected < len(tokens):
            return None
        available = range(len(tokens)) if available is None else available
        self._paths.setdefault(player, []).append({"base": base, "scope": scope,
            "selected": selected, "token": tokens[selected], "hard": hard,
            "available": {tokens[index] for index in available if 0 <= index < len(tokens)},
            "backtrack": snapshot.valid_actions[selected].operation_id in
                (ActionOperation.CANCEL, ActionOperation.UNSELECT, ActionOperation.NO)})
        return epoch

    # 写出失败时保守丢弃同代软证据，旧对局失败回调不能清理新局路径
    def abort(self, epoch):
        if epoch is not None and epoch == self._epoch:
            self.reset()
