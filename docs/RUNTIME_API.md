# 异步对局运行时接口

该接口是 Galatea Link 与 AstrBot、QQ 或其他上层平台之间的稳定边界，不依赖任何具体机器人框架

## 使用方式

```python
import asyncio

from app_config import load_app_config
from core.link_events import EventStreamClosed
from galatea_link import create_link_from_config


# 持续消费对局事件直到连接关闭
async def consume_events(link):
    subscription = link.runtime.subscribe_events(max_queue_size=128)
    try:
        while True:
            event = await subscription.get()
            print(event.to_dict())
    except EventStreamClosed:
        return


# 并行启动游戏连接和外部事件消费者
async def main():
    link = create_link_from_config(load_app_config())
    consumer = asyncio.create_task(consume_events(link))
    await link.start()
    await consumer


asyncio.run(main())
```

## 状态与观察

`link.runtime.get_status()` 返回连接状态、决斗状态、座位、当前介入配置、活动决策编号和最近决策摘要，不包含 API Key、密码或原始网络载荷

`link.runtime.get_latest_observation()` 返回标准玩家可见观察的独立副本。事件中的 `observation_id` 可用于判断是否已经读取到最新版本

## 动态介入控制

```python
await link.runtime.update_intervention(
    mode="hybrid",
    core_confidence_threshold=0.5,
    force_llm_message_types=[13, 16],
)
```

运行中支持 `core_only`、`llm_only`、`llm_review` 和 `hybrid`。更新使用异步锁串行化，不会修改磁盘配置，下一次启动仍以 `config.yaml` 为准

## 外部平台消息

```python
await link.runtime.publish_external_message(
    "用户要求下一回合保守一些",
    source="astrbot.qq",
    sender_id="123456",
)
```

该调用会发布 `external.message.received`，后续对话管理器可以异步消费它，不会阻塞游戏网络循环

当前阶段尚未把外部消息自动写入 LLM 对局上下文，也没有猜测 MDPro3 的游戏内聊天发包格式。这两部分将在对应适配层中显式实现

## 事件类型

连接与房间事件

- `connection.starting`
- `connection.connected`
- `connection.failed`
- `room.joined`
- `room.seat.assigned`
- `duel.started`
- `duel.ended`
- `link.closing`
- `link.closed`

观察与决策事件

- `observation.updated`
- `decision.requested`
- `core.suggested`
- `llm.requested`
- `llm.completed`
- `llm.timed_out`
- `llm.skipped`
- `llm.failed`
- `decision.committed`
- `decision.failed`

消息与控制事件

- `chat.suggested`
- `external.message.received`
- `runtime.intervention.updated`
- `server.error`
- `message.processing_failed`

## 背压行为

每个订阅者拥有独立的有界队列。消费者落后且队列已满时，只会丢弃该消费者最旧的事件，并增加 `subscription.dropped_events`，不会等待消费者或阻塞游戏收包与决策
