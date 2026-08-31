# 对局事件广播模块，负责向多个异步消费者非阻塞分发运行状态

import asyncio
import copy
import time
from dataclasses import dataclass
from typing import Any


_STREAM_CLOSED = object()


class EventStreamClosed(RuntimeError):
    pass


@dataclass(frozen=True)
class LinkEvent:
    sequence: int
    event_type: str
    created_at: float
    payload: dict[str, Any]

    # 转换为适合 JSON 序列化的独立字典
    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "payload": copy.deepcopy(self.payload),
        }


class LinkEventSubscription:
    # 初始化单个异步消费者的有界事件队列
    def __init__(self, bus, max_queue_size: int):
        self._bus = bus
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queue_size)
        self._closed = False
        self.dropped_events = 0

    # 等待并返回下一条对局事件
    async def get(self) -> LinkEvent:
        item = await self._queue.get()
        if item is _STREAM_CLOSED:
            self._closed = True
            raise EventStreamClosed("对局事件流已经关闭")
        return item

    # 关闭当前订阅并解除总线引用
    def close(self) -> None:
        if self._closed:
            return
        self._bus._unsubscribe(self)

    # 非阻塞写入并在拥塞时丢弃最旧事件
    def _offer(self, item: Any) -> None:
        if self._closed:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self.dropped_events += 1
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(item)

    # 标记订阅关闭并唤醒正在等待的消费者
    def _finish(self) -> None:
        if self._closed:
            return
        self._offer(_STREAM_CLOSED)
        self._closed = True


class LinkEventBus:
    # 初始化可同时服务多个外部消费者的事件总线
    def __init__(self):
        self._sequence = 0
        self._subscriptions: set[LinkEventSubscription] = set()
        self._closed = False

    # 创建不会反向阻塞游戏进程的有界订阅
    def subscribe(self, max_queue_size: int = 128) -> LinkEventSubscription:
        if self._closed:
            raise EventStreamClosed("对局事件总线已经关闭")
        if max_queue_size < 1:
            raise ValueError("事件队列容量必须大于 0")
        subscription = LinkEventSubscription(self, max_queue_size)
        self._subscriptions.add(subscription)
        return subscription

    # 向所有订阅者同步投递一条不可变事件快照
    def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> LinkEvent | None:
        if self._closed:
            return None
        if not event_type:
            raise ValueError("事件类型不能为空")
        self._sequence += 1
        event = LinkEvent(
            sequence=self._sequence,
            event_type=event_type,
            created_at=time.time(),
            payload=copy.deepcopy(payload or {}),
        )
        for subscription in tuple(self._subscriptions):
            subscription._offer(copy.deepcopy(event))
        return event

    # 关闭总线并唤醒全部等待中的订阅者
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for subscription in tuple(self._subscriptions):
            subscription._finish()
        self._subscriptions.clear()

    # 移除并关闭指定订阅
    def _unsubscribe(self, subscription: LinkEventSubscription) -> None:
        self._subscriptions.discard(subscription)
        subscription._finish()
