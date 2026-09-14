# AstrBot 远程桥接说明

AstrBot 插件只负责 QQ 等社交平台会话、权限和消息展示，不导入 Torch、ONNX Runtime、卡片知识库或 Link 游戏核心。插件通过端口地址调用 Link 服务，并通过 WebSocket 接收事件

## 使用前提

- 独立启动 Galatea Link，并确认 WebUI 可以读取状态
- 在 AstrBot 插件设置中启用 Galatea Link 接入
- 让 AstrBot 能访问 Link 的 HTTP 地址；跨主机时同时配置访问令牌和网络访问控制
- 在 Link 中准备服务器、卡组和模型；使用 AstrBot 主智能体决策时无需给 Link 配置 LLM API Key

同机部署默认使用 `http://127.0.0.1:8765`。连接正常后，AstrBot Page 应同时显示 HTTP 可达和 WebSocket 已连接，Link WebUI 也会显示 AstrBot 实例名称与最近心跳

## 容器与宿主机

Link 在宿主机、AstrBot 在 Docker 时，Windows Docker Desktop 使用：

```yaml
galatea_link:
  enabled: true
  base_url: "http://host.docker.internal:8765"
  api_token: ""
  api_token_env: "GALATEA_LINK_API_TOKEN"
```

Link 侧监听地址应为 `0.0.0.0`，并设置同名令牌环境变量。插件只保存 Link 访问令牌，不读取 Link 内部的游戏密码或 LLM API Key

跨设备连接同样填写可从 AstrBot 所在环境访问的 Link 地址。`service.allowed_origins` 只限制浏览器跨域来源，不代替令牌、防火墙、VPN 或 HTTPS 反向代理。不要把未加保护的控制端口直接暴露到公网

## 管理员指令

旧插件的管理员指令组为 `决斗AI`：

- `决斗AI 启动` 启动 Link 对局并绑定当前 QQ 会话
- `决斗AI 停止` 停止对局并解除绑定
- `决斗AI 状态` 查看连接、对局、Core 和最近事件
- `决斗AI 配置` 查看脱敏的 YGOPro、Agent、卡组和 LLM 配置
- `决斗AI 模型` 查看模型协议 V3 仓库和下次会话选择
- `决斗AI 选择模型 <文件名>` 选择下次 Link 会话加载的模型
- `决斗AI 模式 <core_only|hybrid|llm_review|llm_only>` 修改介入模式
- `决斗AI 阈值 <0 到 1>` 修改 hybrid 的 Core 置信度阈值
- `决斗AI Core策略 <greedy|deployment>` 修改 Core 动作策略
- `决斗AI Core温度 <0.05 到 5>` 修改 Deployment 动作温度
- `决斗AI 聊天 <文本>` 向游戏内聊天发送消息
- `决斗AI 聊天记录` 查看当前对局聊天历史

`配置`、`模型` 和所有控制更新都使用 Link 的 revision 乐观锁。对局尚未启动时，配置和策略也可以修改；正在运行的模型选择和连接配置会在下一次 Link 会话启动时生效

## AstrBot Pages 设置

插件详情中的 `link-control` Page 现在包含全部 Galatea Link 插件设置，包括启用开关、服务地址、访问令牌、令牌环境变量、请求超时、重连间隔、心跳间隔、实例名称、自动连接、远程主智能体接管、游戏内自动回复、一次性赛后摘要以及各类事件转发开关。访问令牌输入留空会保持现有值，也可以显式勾选清除

Page 会区分插件开关、Link HTTP 可达、WebSocket 事件流、对局任务和当前绑定会话。插件空闲时仍发送心跳，因此 Link WebUI 也能显示 AstrBot 是否已经接入。保存或连接检查完成后，页面右下角会显示明确的成功或失败提示

| 设置 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `false` | 启用 Link 指令、工具和后台连接 |
| `base_url` | `http://127.0.0.1:8765` | Link HTTP 服务地址 |
| `api_token` | 空 | Link 访问令牌；生产环境优先使用环境变量 |
| `api_token_env` | `GALATEA_LINK_API_TOKEN` | AstrBot 进程读取令牌的环境变量名 |
| `request_timeout` | `15` | 普通 HTTP 控制请求超时，不是单次动作预算 |
| `auto_connect` | `true` | 插件加载后自动建立事件流并发送心跳 |
| `instance_name` | `AstrBot` | Link WebUI 中显示的实例名称 |
| `heartbeat_interval` | `10` | 心跳秒数，允许 3 至 30 秒 |
| `reconnect_delay` | `3` | WebSocket 意外断开后的重连间隔 |
| `remote_agent_enabled` | `true` | 允许 AstrBot 主智能体接管需要远程判断的动作 |
| `agent_game_chat_enabled` | `true` | 允许主智能体回复去重后的对手新消息 |
| `inject_duel_summary` | `true` | 在同一会话下一次普通聊天中注入一次轻量摘要 |
| `allow_agent_deck_edit` | `false` | 允许主智能体赛前修改当前会话临时卡组 |
| `forward_*` | `true` | 分别控制聊天、生命周期、错误和发言建议转发 |

建议首次连接按以下顺序操作：保存 Page 设置、执行连接检查、用 `/决斗AI 状态` 确认双向状态、让主智能体列出卡组并配置会话，最后启动对局。服务地址、令牌或网络发生变化时先重新执行连接检查

## 主智能体工具

| 工具 | 用途 |
| --- | --- |
| `galatea_link_status` | 查看 Link、事件流、对局和模型状态 |
| `galatea_link_last_duel` | 查询当前会话最近一次轻量对局摘要 |
| `galatea_link_configure_session` | 配置服务器端口、密码、卡组和本次决策参数 |
| `galatea_link_list_decks` | 列出 Link 本地卡组和当前会话可见的工具箱卡组 |
| `galatea_link_import_current_deck` | 将当前工具箱缓存同步为会话临时卡组并设为待对战卡组 |
| `galatea_deck_import` | 导入 YDK 文本、YDKe、Ourocg 链接或消息附件 |
| `galatea_card_lookup` | 按卡名或卡密查询卡片文本和可选裁定 |
| `galatea_link_edit_current_deck` | 在显式授权后修改当前临时对战卡组 |
| `galatea_link_start_duel` | 启动已配置的对局并绑定当前 AstrBot 会话 |
| `galatea_link_stop_duel` | 停止当前对局 |
| `galatea_link_observation` | 读取当前玩家可见状态和合法动作 |
| `galatea_link_game_chat` | 向游戏内发送短消息 |
| `galatea_link_submit_decision` | 提交合法动作和可选的临时介入调整 |

插件会把 `galatea_link_configure_session` 配置的决策后端切换为 `remote_astrbot`，但不会改变 Link 已选择的 `core_only`、`hybrid`、`llm_review` 或 `llm_only` 模式。AstrBot 只是替代 Link 本地 LLM 后端：`core_only` 不请求 AstrBot，`hybrid` 只在策略要求介入时请求，`llm_review` 和 `llm_only` 则按各自规则请求。需要判断时，Link 通过事件流发送带有效期的玩家可见观察；AstrBot 在原主智能体上下文内决策，再提交 `request_id`、观察身份和动作。过期、跨会话、观察不匹配或非法动作都会被 Link 拒绝

## 桥接方法

`GalateaLinkBridge` 提供以下异步方法供 AstrBot Pages 或其他插件复用：

- `get_configuration()` 和 `update_configuration(patch, requester_umo=None)`
- `get_models()` 和 `select_model(filename, requester_umo=None)`
- `get_decks(scope_id)` 和 `import_toolbox_deck(...)`
- `get_capabilities()`
- `get_controls()`、`get_latest_observation(requester_umo)`
- `update_controls(requester_umo, patch)`
- `update_core_policy(requester_umo, policy_mode)` 和 `update_core_temperature(requester_umo, temperature)`
- `send_game_chat(requester_umo, text)` 和 `get_game_chat_history(requester_umo, max_messages)`
- `publish_external_message(requester_umo, text, sender_id)` 将 QQ 消息投递为异步事件

所有方法都只传输结构化 JSON。模型导入仍建议从 Link WebUI 或 `deploy_packages` 目录完成，AstrBot 插件暂不上传大型 GKG 文件

外部消息的事件类型为 `external.message.received`。Link 不会因为收到 QQ 消息而额外调用 LLM，也不会把消息直接写入游戏协议；AstrBot 可以根据自己的群聊、私聊和权限策略决定是否转发

工具箱每次成功保存或转存 YDK 后，会在当前事件循环中异步复制到 Link。`galatea_link_list_decks` 只列出 Link 本地卡组和当前群聊或当前用户作用域的缓存卡组，并返回具体卡片名称、投入数量和来源。`galatea_link_configure_session` 接受列表中的 `deck_ref` 或显示名；选择 Link 本地卡组时插件会先复制成当前会话临时副本，再由 Link 校验会话归属

`galatea_deck_import` 供主智能体代为解析 YDK 文本、YDKe、Ourocg 链接或当前消息及引用消息中的 YDK 文件。它复用原工具箱解析和 24 小时清理逻辑，只写入当前会话缓存，并在 Link 可用时立即把同步后的副本设为当前待对战临时卡组，因此导入后可直接调用编辑工具。`galatea_link_configure_session` 也允许只传卡组而沿用现有服务器设置

`galatea_card_lookup` 复用工具箱的百鸽搜索器，支持用卡名或卡密查询卡名、卡密、类型、效果文本、灵摆文本和基础数值，也可以按需返回最匹配卡片的裁定问答

`galatea_link_edit_current_deck` 只在管理员显式开启 `allow_agent_deck_edit` 后可用，并且 Link 会再次校验它只能修改当前会话已选中的 AstrBot 临时卡组。Link 会话尚未启动时，修改在下次启动加载；已经进入大厅但决斗尚未真正开始时，Link 会立即重载实际 YDK，按 YGOPro 标准重新上传主卡、额外卡组和备牌，并重新发送准备。真正进入决斗后仍拒绝修改。修改操作支持 `add`、`remove` 和 `move`，兼容主智能体常用的 `operation` 与 `op` 字段名，目标区域为 `main`、`extra` 或 `side`

服务器拒绝准备时，Link 会解析 `STOC_ERROR_MSG` 中的卡组错误位域，返回禁限卡表、OCG/TCG 限制、未知卡、同名卡数量、主卡数量、额外卡组数量、备牌数量或不可用卡片等原因。卡片类错误会附带卡名与卡密，数量类错误会附带服务端报告数量；该结果会进入 AstrBot 通知以及 `galatea_link_status` 的最近服务器拒绝字段，便于主智能体修改临时卡组后重试

当前模型协议 V3 只覆盖初始卡组提交，没有定义 BO3 局间换备阶段。现阶段可以在开局前编辑 `side` 区域，但不会模拟在线 BO3 换备；该流程等待 Core V4 上层换备模块确定状态与协议后接入

## 游戏聊天与轻量对局记忆

开启 `agent_game_chat_enabled` 后，每条去重后的对手新消息会独立调用当前 AstrBot 主智能体，并将最终短句直接送回 YGOPro。该调用复用当前会话的人格和模型，只开放查卡工具；旧聊天正文会从动作决策载荷中移除，因此不会在后续每个时点重复触发回复。遇到有时限的动作决策时，会取消尚未完成的闲聊生成并优先提交动作

开启 `inject_duel_summary` 后，插件从事件流累计胜负与结束原因、持续时间、动作数量与来源、聊天数量和最后回合阶段。它不会额外调用模型，也不会持久保存完整对局；收到 `duel.result` 时会立即形成摘要，断线或主动停止时则生成胜负未知的摘要，并在同一 AstrBot 会话下一次正常聊天请求中注入一次。如果 AstrBot 重启或插件热重载，尚未消费的摘要会自然丢失

`galatea_link_last_duel` 会在当前会话内返回最近一次摘要，即使一次性自动注入已经被消费也仍可主动查询。该记录只存在插件内存中，并继续沿用 AstrBot 会话隔离

## 事件转发

桥接器会按插件配置将游戏聊天、生命周期事件、决策错误和 LLM 发言建议发送回启动对局的 AstrBot 会话。配置更新事件只用于刷新桥接状态，不会把服务端密钥或原始网络载荷转发到 QQ

事件 WebSocket 断线后会按 `reconnect_delay` 自动重连。插件卸载时只关闭自己的 HTTP/WebSocket 客户端，不会停止独立 Link 服务

## 常见问题

### Page 没有设置或保存反馈

确认安装目录包含 `_conf_schema.json` 和 `link_control_page.py`，然后重载插件。Page 保存后会显示成功或失败提示；旧版缓存仍显示空页面时，先升级插件文件再重载 AstrBot

### HTTP 不可达或返回 401

Docker 中不要用 `127.0.0.1` 指向宿主机 Link，应改用 `host.docker.internal` 或实际可达地址。401 表示两端令牌不一致；检查 `api_token_env` 指向的环境变量，并在修改后重启对应进程

### 动作总在固定秒数超时

`request_timeout` 只控制普通 HTTP 请求。远程动作的完整预算来自 Link 的 `decision.astrbot_time_budget` 或会话覆盖，AstrBot 调用自身模型的超时还必须不小于该预算。修改后用 `/决斗AI 配置` 或 Link WebUI 核对实际生效值

### 额外卡组为空或修改后没有生效

确认 Link 与 AstrBot 插件均已更新。YGOPro 的 `CTOS_UPDATE_DECK` 不使用独立的额外卡组长度字段，而是将主卡组和额外卡组合并计入第一个区域、将备牌放入第二个区域；旧版 Link 会把额外卡组误当成备牌。更新后可调用 `galatea_link_status` 核对 Link 实际加载的主卡、额外卡组和备牌数量

### 游戏聊天出现模型错误文本

聊天回复只发送最终自然语言。如果供应商拒绝带有未配对 `tool_calls` 的历史消息，插件会把失败视为聊天生成错误，而不应把完整异常或工具调用历史发送进游戏。仍出现此问题时，保留 AstrBot 错误日志并检查当前模型供应商是否兼容 AstrBot 的工具消息格式

### 对局结束后主智能体没有记忆

打开 `inject_duel_summary`。摘要只保存在插件内存中，并在同一 AstrBot 会话下一次普通聊天时注入一次；它不会保存完整牌局，插件重启后也不会恢复。也可以让主智能体调用 `galatea_link_last_duel`

## 隐私边界

工具箱卡组按当前 AstrBot 会话散列隔离，Link 不会向其他会话列出卡组或标注来源。选择 Link 本地卡组时先建立当前会话临时副本，智能体编辑不会覆盖原文件。插件不会读取 Link 的 LLM API Key，也不会把房间密码、访问令牌、原始协议载荷或完整对局记录转发到 QQ
