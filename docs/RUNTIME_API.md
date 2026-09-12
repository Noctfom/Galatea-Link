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

`link.runtime.get_status()` 返回连接状态、决斗状态、大厅房主与准备状态、大厅座位 `player_id`、对局内视角 `core_player_id`、双方剩余时间、Core 模型可用状态、版本化适配器与检查点身份、当前介入配置、活动决策编号和最近决斗结果，不包含 API Key、密码、模型路径或原始网络载荷

大厅座位与对局内 Core 玩家编号不能直接比较。猜拳与先后攻确定后，Link 会从 `MSG_START` 建立映射；`duel.player.assigned` 会在映射建立或由完整交互消息校正时发布

`link.runtime.get_latest_observation()` 返回标准玩家可见观察的独立副本。事件中的 `observation_id` 可用于判断是否已经读取到最新版本

## 动态介入控制

所有前端、AstrBot 和其他外部调用方应优先使用统一控制面

```python
current = link.runtime.get_controls()
updated = await link.runtime.update_controls(
    {
        "intervention": {
            "mode": "hybrid",
            "core_policy_mode": "deployment",
            "core_temperature": 0.8,
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
        "game_chat": {
            "enabled": True,
            "capture_incoming": True,
            "send_enabled": True,
            "include_in_llm_context": True,
            "llm_suggestions_enabled": True,
            "auto_send_llm_chat": False,
            "max_context_messages": 12,
            "max_context_chars": 3000,
            "max_outbound_utf16_units": 120,
            "min_auto_send_interval": 15.0,
        },
    },
    source="astrbot.qq",
    expected_revision=current["revision"],
)
```

控制状态使用 `galatea.runtime_controls.v1`，分为当前有效介入设置、人工基线设置、自主介入设置和游戏聊天设置。`revision` 每次宏观策略发生变化时递增，外部调用方可以通过 `expected_revision` 防止前端与 AstrBot 相互覆盖

支持 `core_only`、`llm_only`、`llm_review` 和 `hybrid`。`core_policy_mode: greedy` 使用确定性最高分动作；`deployment` 按 `core_temperature` 的 softmax 分布采样，范围为 0.05 到 5.0，可用于 core-only、hybrid 和其他会执行 Core 推理的模式

直接调用 `link.runtime.update_controls` 只更新当前运行实例。通过 Link 服务的 `PATCH /api/v1/controls` 更新时，即使会话尚未启动也可修改，并把非敏感基线保存到 `link_state.json` 供后续会话复用

## 配置中心边界

独立服务额外提供 `GET /api/v1/configuration` 和 `PATCH /api/v1/configuration`。它们用于保存下一次会话的 YGOPro 服务器地址、协议版本、房间编号、Agent 名称、卡组和 LLM 非敏感参数。响应中的密码和 API Key 永远只表现为配置状态，不含明文

`POST /api/v1/configuration/server/test` 只做 TCP 建连探测，`POST /api/v1/configuration/llm/test` 发送一次最小合法观察并校验结构化动作，因此后者会产生一次供应商 API 请求和费用。两个测试都接受表单草稿，不会自动保存配置

AstrBot 可通过 `POST /api/v1/external-message` 把 QQ 消息投递为 `external.message.received` 事件。Link 只负责异步转发和记录事件，不会自动把社交消息写入游戏协议或强行触发 LLM 请求；社交会话编排由 AstrBot 负责

当决策配置的 `agent_backend` 为 `remote_astrbot` 时，Link 会在需要远程判断的时点发布 `agent.decision.requested`。事件包含 `session_id`、`request_id`、`observation_id`、过期时间、玩家可见观察和合法动作列表。AstrBot 插件应在原有主智能体上下文中调用决斗工具，最后向 `POST /api/v1/decisions/{request_id}` 提交这些身份字段与 `choice_id`。Link 会拒绝过期、跨会话、观察版本不匹配或不在合法列表中的动作

远程会话推荐先调用 `POST /api/v1/session/configure`，在内存中设置服务器地址、端口、密码、卡组和 `agent_backend: remote_astrbot`，再调用启动接口。密码不会进入状态文件或 WebSocket 事件

## 模型协议 V3 运行资产

`GET /api/v1/assets` 返回当前模型目录的 CDB、Lua 脚本、语义资产、本机构建依赖和 MSG 142 宣言兜底池状态

- `POST /api/v1/assets/card-data/sync` 同步并校验 CDB 与 YGOPro Lua 脚本
- `POST /api/v1/assets/semantics/sync` 下载并校验远端已发布的完整语义资产
- `POST /api/v1/assets/semantics/rebuild` 本机解析 Lua 并生成向量，接受可选 `remote_url` 和布尔值 `clear_existing`
- `PATCH /api/v1/assets/meta-staples` 使用 `add`、`remove` 或 `replace` 修改 MSG 142 宣言兜底卡密

卡库和语义资产只允许在 Link 会话停止时替换。142 兜底池可以随时保存，运行中修改会在下一次会话生效。本机语义化是可选重型功能，仅调用该入口时才加载 `requirements-semantic.txt` 中的依赖

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

## 游戏聊天反向接口

Link 只负责游戏内聊天，不处理 QQ 会话语义。AstrBot 插件可以订阅游戏聊天事件，也可以通过运行时接口把自己的输出发回游戏

```python
# 读取最近的游戏聊天历史
history = link.runtime.get_game_chat_history(
    max_messages=12,
    max_chars=3000,
)

# 由 AstrBot 或其他上层显式发回游戏
sent = await link.runtime.send_game_chat(
    "你好，祝对局愉快",
    source="astrbot",
)
```

服务端的合法聊天包会发布 `game_chat.received`。成功发送后会发布 `game_chat.sent`，两种事件都使用 `galatea.game_chat_context.v1` 对应的消息字段，包括方向、角色、来源、文本、`player_type` 和递增序号。己方消息被服务器短时回显时不会重复写入历史，接收事件会通过 `echo_of_sequence` 指向原出站记录

聊天历史只保存在内存中，每次新决斗开始时清空。历史最多保存 100 条和 16000 个字符，实际交给 LLM 的范围再由 `max_context_messages` 与 `max_context_chars` 限制

协议适配基于 YGOPro 的 `CTOS_CHAT=0x16` 与 `STOC_CHAT=0x19`。客户端正文为 UTF-16LE 空结尾文本，服务端正文前包含 2 字节小端 `player_type`。协议编解码集中在 `core/game_chat.py`，具体兼容客户端若有扩展差异可以只替换这一层

`include_in_llm_context` 默认开启，因此下一次动作决策会收到已经记录的游戏聊天。聊天被明确标记为不可信社交内容，不能覆盖系统规则、信息可见性或合法动作

`auto_send_llm_chat` 默认关闭。关闭时 LLM 的 `chat_message` 只发布 `chat.suggested`；开启后才会自动发给游戏服务器，并受到 UTF-16 长度、重复内容和最短发送间隔保护

当前 LLM 调用仍由游戏动作时点触发。对手单独发送聊天不会额外创建一次 LLM 请求，因此即时闲聊应先由 AstrBot 订阅反向接口处理，或在后续阶段增加独立聊天调度器

可用以下命令进行真实 YGOPro 协议聊天测试。进入房间并分配座位后，终端普通文本会通过运行时接口发送，`/history` 查看当前历史，`/quit` 安全退出

```powershell
python scripts/test_game_chat_online.py
```

## 通用外部消息事件

```python
await link.runtime.publish_external_message(
    "用户要求下一回合保守一些",
    source="astrbot.qq",
    sender_id="123456",
)
```

该调用会发布 `external.message.received`，后续对话管理器可以异步消费它，不会阻塞游戏网络循环

该接口为已有上层调用保留，但不会自动写入 LLM 对局上下文。QQ 消息的会话管理、权限和平台回复由 AstrBot 插件负责

## 事件类型

连接与房间事件

- `connection.starting`
- `connection.connected`
- `connection.failed`
- `room.joined`
- `room.seat.assigned`
- `room.role.updated`
- `room.player.updated`
- `room.start.requested`
- `duel.started`
- `duel.player.assigned`
- `duel.time.updated`
- `duel.result`
- `duel.ended`
- `link.closing`
- `link.closed`

观察与决策事件

- `observation.updated`
- `decision.requested`
- `agent.decision.requested`
- `agent.decision.closed`
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
- `game_chat.received`
- `game_chat.sent`
- `game_chat.decode_failed`
- `game_chat.auto_send_skipped`
- `game_chat.auto_send_failed`
- `game_chat.history_cleared`
- `external.message.received`
- `runtime.controls.updated`
- `service.configuration.updated`

`duel.result` 在解析到 Core `MSG_WIN` 时立即提供己方视角的 `outcome`、赢家、原因码、原因说明和结束类型。`duel.ended` 在服务端结束包、断线或主动停止时统一发布；没有收到胜负消息时会明确使用 `outcome: unknown`，不会猜测赢家

Link 处于房主身份时会维护大厅准备状态。在单人对战的 0、1 号决斗者均准备后自动发送标准 `CTOS_HS_START`，并以 `room.start.requested` 暴露本次自动开始请求
- `service.llm_secret.updated`
- `integration.astrbot.connected`
- `integration.astrbot.disconnected`
- `runtime.controls.failed`
- `runtime.autonomy.progressed`
- `runtime.autonomy.ignored`
- `server.error`
- `message.processing_failed`

## 背压行为

每个订阅者拥有独立的有界队列。消费者落后且队列已满时，只会丢弃该消费者最旧的事件，并增加 `subscription.dropped_events`，不会等待消费者或阻塞游戏收包与决策
