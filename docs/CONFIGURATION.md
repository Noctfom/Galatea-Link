# Galatea Link 配置参考

本页说明 `config.yaml` 的全部公开配置、持久化优先级和常见组合。首次运行先复制根目录的 `config.example.yaml`，不要直接把真实密码或 API Key 写回公开模板

## 配置文件与优先级

Link 服务启动时先读取 `config.yaml`，然后读取同目录下的本地状态文件

1. `config.yaml` 提供基础值
2. `link_state.json` 覆盖 WebUI 可编辑的非敏感配置、决策策略、模型选择和 revision
3. `link_secrets.json` 提供 WebUI 本机保存的 LLM API Key
4. `GALATEA_LLM_API_KEY` 非空时优先于本地密钥文件和 YAML 中的 LLM Key

服务访问令牌稍有不同：`service.api_token` 非空时直接使用该值，否则读取 `service.api_token_env` 指定的环境变量。公开部署建议让 YAML 中的 `api_token` 保持为空，只使用环境变量

服务器房间密码只应放在本机 `config.yaml`，或由 AstrBot 通过临时会话配置提交。临时房间密码只保存在 Link 进程内，不写入 `link_state.json`

如果修改 `config.yaml` 后界面仍显示旧值，通常是 `link_state.json` 中已经存在覆盖。可以在 WebUI 修改并保存，或停止服务后备份并移走该状态文件

## `server` 游戏服务器

| 字段 | 默认或示例 | 说明 |
| --- | --- | --- |
| `profile` | `ygopro` | 网络协议配置名称，当前主线使用 YGOPro |
| `host` | `127.0.0.1` | 游戏服务器主机名或 IP |
| `port` | `7911` | 游戏服务器 TCP 端口，范围 1–65535 |
| `password` | 空 | 房间密码，属于敏感信息 |
| `protocol_version` | `0x1361` | 加入房间时声明的客户端协议版本，支持十进制或十六进制 |
| `auto_negotiate_version` | `true` | 收到服务端版本拒绝时，使用服务端要求版本有限重连 |
| `max_version_retries` | `1` | 自动版本重试上限，范围 0–3 |
| `game_id` | `0` | YGOPro 加入载荷中的房间编号 |
| `connect_timeout` | `10.0` | TCP 建连超时秒数 |
| `trace_packets` | `false` | 输出协议包类型与长度诊断，不输出密钥 |

协议自动协商只影响当前连接，不会把服务端要求版本写回基础配置。若某个服务长期固定版本，可以在验证后手动更新 `protocol_version`

## `service` Link 控制服务

| 字段 | 默认或示例 | 说明 |
| --- | --- | --- |
| `host` | `127.0.0.1` | HTTP/WebSocket 监听地址 |
| `port` | `8765` | Link WebUI 和 API 端口 |
| `api_token` | 空 | 明文访问令牌，不建议写入文件 |
| `api_token_env` | `GALATEA_LINK_API_TOKEN` | 访问令牌环境变量名 |
| `event_queue_size` | `256` | 每个事件订阅的有界队列容量，范围 8–4096 |
| `webui_enabled` | `true` | 是否提供内置 WebUI 静态页面 |
| `allowed_origins` | `[]` | 允许跨域访问 API 的完整 Origin 列表 |

只在本机使用时保持 `127.0.0.1`。AstrBot 位于 Docker 或另一台机器时，将监听地址改为 `0.0.0.0`，同时必须配置访问令牌，并使用操作系统防火墙、内网或 VPN 限制来源

`allowed_origins` 只控制浏览器跨域请求，不是网络访问控制，也不能代替 Token

## `agent` 游戏身份

| 字段 | 默认或示例 | 说明 |
| --- | --- | --- |
| `name` | `Galatea_AI` | 游戏内名称，最多 20 个 UTF-16 编码单元 |
| `deck` | `example` | `decks/` 下不含 `.ydk` 的卡组名，或服务生成的卡组引用 |
| `prefer_second` | `false` | 猜拳获胜后是否选择后攻；`false` 表示让自己先攻 |

本地卡组和 AstrBot 导入卡组不会进入 Git。Link WebUI 的“卡组仓库”可以导入、查看、选择、按卡密增删或移动单卡，并删除当前配置未使用的本地 YDK；正在对局时禁止修改。AstrBot 卡组按会话哈希目录隔离，并有单独的过期清理逻辑

## `model` Core 模型

| 字段 | 默认或示例 | 说明 |
| --- | --- | --- |
| `device` | `cpu` | PyTorch 设备，例如 `cpu` 或 `cuda` |
| `weights_path` | `./models/galatea_model.onnx` | PTH 或 ONNX 主制品路径 |
| `expected_model_id` | 空 | 可选模型 UUID 锁定，非空时不匹配即拒绝加载 |
| `protocol` | `auto` | `auto` 或显式模型协议，当前推荐 `auto`/`v3` |
| `inference_backend` | `auto` | `auto`、`pytorch` 或 `onnxruntime` |
| `onnx_providers` | `CPUExecutionProvider` | ONNX Runtime 执行提供器列表 |
| `onnx_intra_op_threads` | `0` | ONNX 单算子线程数，0 表示自动 |
| `assets_path` | 空 | 运行资产目录，空值时自动寻找 `model_assets/v3` |
| `strict_asset_hashes` | `true` | 严格校验模型声明的语义资产哈希 |
| `config` | 网络结构字段 | 旧制品或显式构造网络时使用，优先以制品协议元数据为准 |

模型文件、外部数据文件和 GKG 默认被 `.gitignore` 排除。公共仓库只保留加载代码，不应把训练权重随源码意外上传

模型协议是兼容边界，不等同于某个 Galatea Core 发布版本。同为 V3 的后续 Core 版本继续使用 V3 适配器；只有制品协议真正升级时才新增目录

## `llm` Link 内置 LLM

| 字段 | 默认或示例 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否允许 Link 直接调用 LLM |
| `provider` | `openai_compatible` | 当前支持的接口类型 |
| `base_url` | 供应商 `/v1` 地址 | 不包含 `/chat/completions` 的 API 根地址 |
| `api_key` | 空 | 明文 Key 兼容字段，不建议使用 |
| `api_key_env` | `GALATEA_LLM_API_KEY` | 优先读取的环境变量名 |
| `model` | 空 | 供应商模型名称 |
| `timeout` | `30.0` | Link 到 LLM API 的单次 HTTP 请求超时 |
| `max_tokens` | `256` | 最大输出 Token，不限制输入观察 |
| `temperature` | `0.1` | LLM 文本采样温度，范围 0–2 |
| `response_format` | `json_object` | `json_object`、`json_schema` 或 `none` |
| `trace_requests` | `false` | 记录耗时、Token、缓存和返回字符数，不记录 Key |
| `thinking_mode` | `disabled` | `auto`、`enabled` 或 `disabled` |
| `cache_static_context` | `true` | 把稳定规则与卡组知识放在固定前缀以争取缓存命中 |
| `cache_deck_text` | `false` | 把整副卡组文本加入固定前缀，首次输入会明显增大 |
| `compact_dynamic_observation` | `true` | 删除动态观察里已经存在于固定前缀的内容 |

当 `decision.agent_backend` 为 `remote_astrbot` 时，Link 不调用这里配置的模型，但仍保留配置供切回本地智能体

## `decision` 决策与介入

| 字段 | 默认或示例 | 说明 |
| --- | --- | --- |
| `mode` | `core_only` | `core_only`、`hybrid`、`llm_review` 或 `llm_only` |
| `agent_backend` | `local` | `local` 或 `remote_astrbot` |
| `core_policy_mode` | `greedy` | `greedy` 取最高分，`deployment` 按温度采样 |
| `core_temperature` | `0.8` | Core deployment 温度，范围 0.05–5 |
| `core_confidence_threshold` | `0.65` | hybrid 模式触发 LLM 的 Core 置信度阈值，范围 0–1 |
| `force_llm_message_types` | `[]` | hybrid 下无视置信度、强制交给 LLM 的 OCG 类型 |
| `include_core_suggestion` | `true` | 向 LLM 提供 Core 选择、置信度和概率间隔 |
| `llm_time_budget` | `12.0` | 单个游戏动作等待本地 LLM 或 AstrBot 的总预算 |

`llm_time_budget` 包含 AstrBot 等锁、模型生成、工具执行和动作提交，不等于 `llm.timeout`。预算到期后旧请求失效，Link 会使用 Core 或 RuleBot 回退

自主调整字段：

| 字段 | 说明 |
| --- | --- |
| `autonomous_intervention_enabled` | 是否接受 LLM 随动作返回的临时宏观调整 |
| `autonomous_allowed_modes` | LLM 被允许切换到的模式 |
| `autonomous_core_confidence_min/max` | LLM 可设置的 Core 阈值边界 |
| `autonomous_max_ttl_decisions` | 单次调整最多影响的后续决策数 |
| `autonomous_max_force_message_types` | 单次调整最多强制的消息类型数量 |

自主调整默认关闭。打开后仍受 revision、TTL 和人工边界校验，不能改变当前已经提交的动作

## `game_chat` 游戏内聊天

| 字段 | 默认或示例 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 游戏聊天总开关 |
| `capture_incoming` | `true` | 记录服务器下发的聊天 |
| `send_enabled` | `true` | 允许外部接口向游戏发送聊天 |
| `include_in_llm_context` | `true` | 将有界聊天作为不可信社交上下文提供给本地 LLM |
| `llm_suggestions_enabled` | `true` | 接受动作结果中的 `chat_message` 建议 |
| `auto_send_llm_chat` | `false` | 自动发送本地 LLM 的聊天建议 |
| `max_context_messages` | `12` | 提供给 LLM 的最近消息数，范围 1–100 |
| `max_context_chars` | `3000` | 聊天上下文字符上限，范围 1–16000 |
| `max_outbound_utf16_units` | `120` | 单条发言 UTF-16 单元上限，协议最大 255 |
| `min_auto_send_interval` | `15.0` | 自动发言最短间隔秒数 |

聊天历史只保存在内存中，新对局开始时清空。AstrBot 自动回复使用 AstrBot 自己的人格和有界纯文本会话上下文，并过滤悬空工具调用与模型内部错误

## 常见组合

纯 ONNX Core：

```yaml
model:
  protocol: "v3"
  inference_backend: "onnxruntime"
decision:
  mode: "core_only"
  agent_backend: "local"
```

Link 内置 LLM 复核：

```yaml
llm:
  enabled: true
decision:
  mode: "llm_review"
  agent_backend: "local"
```

AstrBot 主智能体：

```yaml
decision:
  mode: "llm_only"
  agent_backend: "remote_astrbot"
  llm_time_budget: 30.0
```

AstrBot Pages 或工具在启动前提交的临时会话配置会覆盖本次连接的服务器、密码、卡组和决策设置，但不会写入公开配置
