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

所有前端、AstrBot 和其他外部调用方应优先使用统一控制面

```python
current = link.runtime.get_controls()
updated = await link.runtime.update_controls(
    {
        "intervention": {
            "mode": "hybrid",
            "core_confidence_threshold": 0.5,
            "force_llm_message_types": [13, 16],
            "include_core_suggestion": True,
            "llm_time_budget": 12.0,
        },
        "autonomy": {
            "enabled": True,
            "allowed_modes": ["core_only", "hybrid", "llm_review"],
            "core_confidence_min": 0.2,
            "core_confidence_max": 0.9,
            "max_ttl_decisions": 3,
            "max_force_message_types": 8,
        },
    },
    source="astrbot.qq",
    expected_revision=current["revision"],
)
```

控制状态使用 `galatea.runtime_controls.v1`，分为当前有效介入设置、人工基线设置和自主介入设置。`revision` 每次宏观策略发生变化时递增，外部调用方可以通过 `expected_revision` 防止前端与 AstrBot 相互覆盖

运行中支持 `core_only`、`llm_only`、`llm_review` 和 `hybrid`。更新使用异步锁串行化，不会修改磁盘配置，下一次启动仍以 `config.yaml` 为准

`update_intervention` 仍作为旧调用方式保留，但内部同样经过统一控制面

## LLM 自主介入

`decision.autonomous_intervention_enabled` 是总开关，默认值为 `false`

开关关闭时，LLM 返回的所有 `intervention_update` 都会被忽略并发布 `runtime.autonomy.ignored`

开关开启后，LLM 可以在正常动作结果中顺带提出以下临时调整，不产生第二次 API 请求

- 后续介入模式
- Core 置信度阈值
- 强制交给 LLM 的 OCG 消息类型
- 调整持续的后续决策数量

每次调整必须回传它看到的 `base_revision`。如果前端或 AstrBot 在模型生成期间修改了控制面，迟到的模型建议会因版本不一致被忽略

单次调整受允许模式、置信度上下界、强制时点数量和最大 TTL 约束。有效期内不会接受新的 LLM 覆盖续期，TTL 到期后自动恢复最新人工基线，任何外部控制更新也会立即取消旧的自主覆盖

自主监督目前采用随决策调用附带的方式，因此不会增加请求成本。`core_only` 模式不会调用 LLM，也就不会产生新的自主建议；需要持续监督时应使用 `llm_review`，需要按 Core 置信度节省调用时使用 `hybrid`

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
- `runtime.controls.updated`
- `runtime.controls.failed`
- `runtime.autonomy.progressed`
- `runtime.autonomy.ignored`
- `server.error`
- `message.processing_failed`

## 背压行为

每个订阅者拥有独立的有界队列。消费者落后且队列已满时，只会丢弃该消费者最旧的事件，并增加 `subscription.dropped_events`，不会等待消费者或阻塞游戏收包与决策
