# 公开连锁进度跟踪，只记录服务器通知且与模型原生历史分离

from collections import deque
from copy import deepcopy


class PublicChainProgress:
    # 当前连锁按编号维护，近期公开发动最多保留八项
    def __init__(self):
        self.current = {}
        self.recent = deque(maxlen=8)
        self.unmatched_notifications = 0
        self.observed = False

    # 从已解析的公开发动建立独立记录，不修改模型连锁或历史字典
    def begin(self, record):
        number = record["ct"]
        self.observed = True
        if not 1 <= number <= 255 or number in self.current:
            self.unmatched_notifications += 1
            return
        item = {**record, "stage": "announced", "chain_ended": False,
            "activation_negated_observed": False, "effect_disabled_observed": False,
            "resolution_observed": False}
        self.current[number] = item
        self.recent.appendleft(item)

    # 分开处理发动确认、结算和两类无效通知，不从未收到的消息推断效果成功
    def observe(self, kind, number=None):
        if kind == 74:
            for item in self.current.values():
                item["chain_ended"] = True
            self.current.clear()
            return
        item = self.current.get(number)
        if item is None:
            self.unmatched_notifications += 1
            return
        if kind in (71, 72, 73):
            item["stage"] = {71: "confirmed", 72: "resolving", 73: "resolved"}[kind]
            if kind == 73:
                item["resolution_observed"] = True
        elif kind == 75:
            item["activation_negated_observed"] = True
        elif kind == 76:
            item["effect_disabled_observed"] = True

    # 返回独立副本，历史快照不能被后续连锁结算反向修改
    def snapshot(self):
        return deepcopy({"observed": self.observed, "current": list(self.current.values()),
            "recent": list(self.recent), "unmatched_notifications": self.unmatched_notifications})
