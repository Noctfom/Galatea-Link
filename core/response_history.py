# 在线响应历史，记录本方真实发送与后续可见消息且不推测对手决策

from collections import Counter, deque
from dataclasses import dataclass
from numbers import Integral, Real
import math
from core.response_codec import encode_int_response


PUBLIC_EVENT_TYPES = frozenset({40, 41, 50, 53, 54, 55, 56, 60, 61, 62, 63, 64, 65,
    70, 71, 72, 73, 74, 75, 76, 83, 90, 91, 92, 93, 94, 95, 96, 97,
    100, 101, 102, 110, 111, 112, 113, 114, 120, 160, 165})
MAX_CAPTURE_MESSAGES = 256
MAX_CAPTURE_BYTES = 256 * 1024


@dataclass(frozen=True)
class VisibleStateDigest:
    lp_p0: int
    lp_p1: int
    zone_counts: tuple
    chain_depth: int


@dataclass(frozen=True)
class ObservedTransition:
    sequence: int
    request_id: int
    actor: int
    prompt_type: int
    turn: int
    phase: int
    action: tuple | None
    choice_id: int | None
    before: VisibleStateDigest
    after: VisibleStateDigest
    boundary: str
    messages: tuple
    truncated: bool


# 冻结动作事实，避免后续修改候选或宏动作列表污染已发送记录
def freeze_action(action):
    if action is None:
        return None
    # 只保存协议允许的标量和列表，不接受可执行对象或任意扩展属性
    def freeze(value):
        if value is None or isinstance(value, (str, bytes, bool)):
            return value
        if isinstance(value, Integral):
            return int(value)
        if isinstance(value, Real) and math.isfinite(value):
            return float(value)
        if isinstance(value, (list, tuple)):
            return tuple(freeze(item) for item in value)
        raise ValueError("动作历史包含不支持的字段类型")
    from data_types import GameAction
    names = {*GameAction.__dataclass_fields__, "summon_method_id", "explicit_response", "sort_prompt_indices"}
    return tuple((name, freeze(getattr(action, name))) for name in sorted(names) if hasattr(action, name))


# 按当前网络响应约定比较字节，无法识别的响应不猜测其候选
def response_bytes(response):
    if response is None:
        return encode_int_response(-1)
    if isinstance(response, Integral):
        return encode_int_response(response)
    if isinstance(response, (bytes, bytearray)):
        return bytes(response)
    raise ValueError("无法识别响应类型")


class OnlineResponseHistory:
    # 初始化有界历史，发送中的草稿在传输确认前不对快照可见
    def __init__(self):
        self._epoch = 0
        self._sequence = 0
        self._records = deque(maxlen=16)
        self._pending = None
        self._gaps = Counter()
        self.perspective = None

    # 重置对局并使上一局异步发送令牌失效
    def reset(self):
        self._epoch += 1
        self._sequence = 0
        self._records.clear()
        self._pending = None
        self._gaps.clear()
        self.perspective = None

    # 标记真正收到服务器消息的物理座位
    def bind(self, perspective):
        if perspective not in (0, 1):
            raise ValueError("响应历史缺少有效玩家视角")
        self.perspective = perspective

    # 在网络等待前创建草稿，使等待期间到达的消息仍能按顺序归属
    def prepare(self, *, request_id, actor, prompt_type, turn, phase, action, choice_id, digest):
        if actor != self.perspective:
            raise ValueError("响应历史玩家与接收视角不一致")
        if self._pending is not None:
            self.abandon("overlapping_submission")
        self._sequence += 1
        token = (self._epoch, self._sequence)
        try:
            action_evidence = freeze_action(action)
        except ValueError:
            action_evidence = None
        self._pending = {"token": token, "request_id": request_id, "actor": actor,
            "prompt_type": prompt_type, "turn": turn, "phase": phase,
            "action": action_evidence, "choice_id": choice_id, "before": digest,
            "after": None, "boundary": None, "messages": [], "bytes": 0,
            "truncated": False, "sent": False}
        return token

    # 只确认仍属于当前对局的草稿，确认发送不等于服务器已接受动作
    def confirm(self, token):
        if self._pending is None or self._pending["token"] != token:
            return False
        self._pending["sent"] = True
        self._publish_if_ready()
        return True

    # 取消失败或未确认的发送，旧回调不能删除新请求的草稿
    def abort(self, token, reason="send_unconfirmed"):
        if self._pending is not None and self._pending["token"] == token:
            self.abandon(reason)

    # 记录来源缺口，不把断线或协议重建伪装为正常完成
    def abandon(self, reason):
        if self._pending is not None:
            self._gaps[reason] += 1
            self._pending = None

    # 区域重建使旧上下文失效并保留可供诊断的缺口计数
    def invalidate(self):
        self._pending = None
        self._records.clear()
        self._gaps["state_rebuild"] += 1

    # 消费可见事件，查询和读取快照不生成历史边界
    def observe(self, kind, payload, digest, *, interaction=False):
        if kind in (35, 162):
            self.invalidate()
            return
        pending = self._pending
        if pending is None or pending["boundary"] is not None:
            return
        if kind in PUBLIC_EVENT_TYPES:
            if len(pending["messages"]) < MAX_CAPTURE_MESSAGES and pending["bytes"] + len(payload) <= MAX_CAPTURE_BYTES:
                pending["messages"].append((kind, bytes(payload), digest.chain_depth))
                pending["bytes"] += len(payload)
            else:
                pending["truncated"] = True
        boundary = "retry" if kind == 1 else "terminal" if kind == 5 else "next_own_prompt" if interaction else None
        if boundary is not None:
            pending["boundary"], pending["after"] = boundary, digest
            self._publish_if_ready()

    # 在发送和后续边界均有证据时发布不可变记录
    def _publish_if_ready(self):
        pending = self._pending
        if pending is None or not pending["sent"] or pending["boundary"] is None:
            return
        self._records.append(ObservedTransition(pending["token"][1], pending["request_id"],
            pending["actor"], pending["prompt_type"], pending["turn"], pending["phase"],
            pending["action"], pending["choice_id"], pending["before"], pending["after"],
            pending["boundary"], tuple(pending["messages"]), pending["truncated"]))
        self._pending = None

    # 返回不可变事实记录，不结算边界或复制整局原始报文
    def snapshot(self):
        return tuple(self._records)

    # 对外只提供计数与缺口，不泄露响应字节卡密或私有候选
    def coverage(self):
        return {"source": "own_sent_response_to_next_own_prompt", "record_count": len(self._records),
            "pending": self._pending is not None, "unmapped_actions": sum(item.action is None for item in self._records),
            "truncated_records": sum(item.truncated for item in self._records), "gaps": dict(self._gaps),
            "opponent_decision_boundaries_available": False, "model_history_complete": False}
