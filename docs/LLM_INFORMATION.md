# LLM 对局信息契约

本文档描述当前版本实际发送给 LLM 的信息、可见性边界和已知缺口

## 基本原则

- LLM 只能接收当前玩家可见的信息
- 对手隐藏卡片只保留区域、序号、表示形式和数量，不发送卡密、名称、属性或效果文本
- 曾经公开的对手手牌作为无序记忆保存，不绑定到具体手牌槽位
- 合法动作只在轮到当前玩家时发送
- LLM 只能返回 `legal_actions` 中存在的 `choice_id`
- 未进入 `DuelState` 的协议信息不会被假定或补全

## 当前完整提供的信息

- 当前事件类型
- 回合数、阶段和行动玩家
- 双方 LP
- 双方手牌、卡组、墓地、除外和额外卡组数量
- 己方手牌与场上卡片身份
- 双方墓地和其他公开卡片身份
- 公开卡片的当前攻击力、防御力、等级、种族、属性和类型
- 公开卡片及当前合法动作涉及卡片的名称与数据库效果文本
- 己方剩余主卡组和额外卡组的卡名与数量
- 当前卡组的 `deck_ref`、显示名和不含会话标识的来源标签
- 已公开过的对手手牌无序记忆
- 当前已记录的连锁
- 最近已记录的公开发动历史
- 当前合法动作、目标实体和选择编号
- 可解析的合法动作效果选项文本
- 公开超量素材的逐张身份与叠放顺序
- 装备来源、装备对象、取对象和被取对象关系
- 公开卡片在本回合已经记录的效果发动位掩码
- 复杂选卡、祭品、求和、指示物、格子和排序请求的完整宏动作候选
- `llm_review` 与 `hybrid` 模式下的 Core 建议、最高动作概率和概率差值
- 开启 `game_chat.include_in_llm_context` 后的最近游戏聊天历史

## 当前部分可靠的信息

- 己方剩余卡组列表依赖移动和抽卡消息被正确解析
- 对手已知手牌记忆依赖公开、移动和离手消息被正确解析
- 当前攻击力、防御力和表示形式只包含状态机已经处理的变更消息
- 连锁历史目前偏向记录公开发动，不是完整的效果处理日志
- 卡片效果文本来自本地 `cards.cdb`，其语言和版本取决于当前数据库
- 卡片效果描述支持旧式与新版 `aux.Stringid`，系统级描述编号仍可能只有原始数值
- 游戏聊天的 `player_type` 可以区分己方、其他玩家、观察者或系统来源，但服务器自定义颜色和扩展身份可能只能归为系统消息
- 效果发动位掩码当前根据 `description_id` 低位映射到八个槽位，只表示状态机已观察到的发动事件，不等同于完整的一回合一次规则判定
- 复杂宏动作候选由规则枚举生成，候选超过 120 个时使用 Core 第一阶段动作概率加权缩减；没有 Core 时使用均匀权重

## 当前尚未完整提供的信息

- 部分系统级 `description_id` 对应的本地化文本
- 不通过装备或取对象消息表达的其他卡片关系
- 卡片效果无效状态与效果适用状态
- 持续到回合结束或指定阶段的残留效果
- 当前特殊召唤、攻击和发动限制
- 玩家提示、客户端提示和部分系统字符串
- 完整的伤害步骤、连锁结算和效果处理历史
- 胜负概率、未知卡概率分布和经过校准的 Core 置信度

## 卡片知识范围

当前请求会为场上、手牌、公开区域、连锁、近期历史和合法动作涉及的卡片提供效果文本

己方初始主卡组和额外卡组会按卡密汇总为固定上下文，包含名称和投入数量。动态观察中的己方剩余卡组只需重复发送卡密和剩余数量

`llm.cache_deck_text` 默认为 `false`。此时固定上下文不携带整副卡组效果，动态 `card_catalog` 仍会发送当前局面和合法动作实际涉及卡片的效果文本

将 `llm.cache_deck_text` 设为 `true` 后，整副己方卡组的效果文本进入固定上下文，后续动态 `card_catalog` 会删除这些重复文本。首次请求会明显增大，但支持前缀缓存的服务端可在后续请求复用

## 实际请求结构

每次 Chat Completions 请求按以下顺序组织消息

1. 固定系统规则，包括可见性约束、合法动作约束和 JSON 输出要求
2. 固定对局上下文，包括智能体名称、初始卡组和观察字段约定
3. 当前动态观察，包括资源、公开卡片、已知信息、连锁、历史、合法动作、可选 Core 建议、`runtime_controls` 和可选 `game_chat_context`

前两条消息在同一副卡组和配置下保持逐字节稳定，动态观察始终放在最后。`latest_observation` 和 AstrBot 后续可读取的标准观察不会被压缩，压缩只发生在实际 API 请求副本中

动态观察中的 `own_deck_identity` 标记当前卡组来源。Link 本地卡组显示为 `link_local`；工具箱同步卡组显示为 `astrbot_toolbox` 和 `current_session`，不会包含原始群号、用户号或会话 ID。AstrBot 主智能体还可调用 `galatea_link_list_decks` 查看当前会话允许选择的完整卡组内容

`game_chat_context` 只在开关开启且历史非空时出现，当前包含以下内容

- 固定模式版本 `galatea.game_chat_context.v1`
- `untrusted_social_context` 信任标记
- 不得覆盖规则、可见性和合法动作的约束
- 受消息数与字符数双重限制的聊天记录
- 每条记录的时间、方向、角色、来源、文本、`player_type` 和关联决策编号

游戏聊天属于动态内容，不会进入固定缓存前缀。没有新聊天时历史仍会随决策重复发送，因此上下文上限应保持较小；默认最多 12 条和 3000 个字符

## 缓存与用量诊断

`llm.cache_static_context` 控制是否发送固定卡组前缀，`llm.compact_dynamic_observation` 控制是否删除前缀已经提供的重复字段

请求日志会输出 `dynamic_chars`、`static_chars`、`saved_chars` 和稳定的 `cache_key`

响应日志兼容以下缓存统计格式

- DeepSeek 的 `prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`
- OpenAI 风格的 `prompt_tokens_details.cached_tokens` 或 `input_tokens_details.cached_tokens`
- 其他兼容服务常见的 `cache_read_input_tokens`

日志中的 `cache_hit_rate` 才是服务端真实命中的结果。固定前缀只为服务端创造可命中条件，不能保证服务端一定实现或保留缓存

可使用以下命令直接验证三次相同请求。首次请求通常用于建立缓存，后续请求观察 `cache_hit_tokens` 和 `cache_hit_rate`

```powershell
python scripts/test_llm_api.py --repeat 3 --interval 2
```

## 实时延迟配置

`llm.thinking_mode` 支持 `auto`、`enabled` 和 `disabled`。实时对局建议使用 `disabled`，避免模型默认高强度推理耗尽输出 token 和当前配置的决策预算

`llm.max_tokens` 当前为 `256`，只约束模型生成内容，不截断输入观察。`decision.llm_time_budget` 是游戏侧独立预算，出现“超过决策时间预算”表示本地在该秒数后取消等待，并不等同于 API 的 `llm.timeout`

当前有三层名称相近但互不替代的超时：`decision.llm_time_budget` 控制游戏每个动作等待本地 LLM 或 AstrBot 的总时长，`llm.timeout` 只控制 Link 直连模型供应商的 HTTP 请求，AstrBot 插件的 `request_timeout` 只控制插件访问 Link HTTP 接口。远程 AstrBot 决策仍以第一项为最终期限

WebUI 修改决策总预算后会写入 `link_state.json`，该持久状态优先于 `config.yaml` 的同名基线。实际生效值可以在 Link 状态的 `decision.llm_time_budget` 或 AstrBot Pages 的“游戏决策总预算”查看

## 决策输出结构

LLM 输出包含 `choice_id`、`reason`、`chat_message` 和可选的 `intervention_update`

`chat_message` 只是动作结果附带的聊天建议。`game_chat.llm_suggestions_enabled` 关闭时运行时忽略该字段；`game_chat.auto_send_llm_chat` 关闭时只发布 `chat.suggested`，开启时才会通过游戏协议发送

自动发送会按 `max_outbound_utf16_units` 截断到完整字符，并执行重复内容与 `min_auto_send_interval` 节流。外部调用 `runtime.send_game_chat` 不会静默截断，超限时会直接报错，方便 AstrBot 在发送前给出明确提示

`intervention_update` 只用于后续决策，不能改变已经选择的当前动作。它包含介入模式、Core 置信度阈值、强制 LLM 时点、TTL、调整理由和控制面基准版本

自主介入总开关关闭时该字段必须为 `null`。即使模型违反要求返回调整，运行时仍会拒绝应用

## 完整度结论

当前输入已经覆盖基础动作语义、公开素材、常见卡片关系、可缓存卡组知识、受控自主介入和有界游戏聊天历史，但仍不足以保证复杂竞技决策始终有效

QQ 等平台对话不会进入 Link 的 LLM 上下文，由 AstrBot 插件独立管理。游戏聊天目前只随动作决策调用 LLM，对手发言本身不会触发独立模型请求；若需要无动作时点的即时自主闲聊，应增加与决策队列分离的低优先级聊天调度器
