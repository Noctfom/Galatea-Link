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
- 公开卡片的已知动态属性，以及本地卡库中的静态身份资料
- 公开卡片及当前合法动作涉及卡片的名称与数据库效果文本
- 己方剩余主卡组和额外卡组的卡名与数量
- 当前卡组的 `deck_ref`、显示名和不含会话标识的来源标签
- 已公开过的对手手牌无序记忆
- 当前已记录的连锁
- 最近已记录的公开发动历史
- 连锁确认、正在结算、结算通知已收到，以及发动无效／效果无效的分别记录
- Link 实际收到的卡片数值提示、描述引用、玩家提示及近期错过发动时点通知
- 明确公开的卡组位置记忆，以及已知顶卡抽入手牌后的无序记忆
- 实际收到的卡片确认／展示身份，包括尚未进场的衍生物；展示面板开关不影响已知身份，未进场卡片只列为近期展示和卡片资料，不提前放入场上实体
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
- 当前攻击力、防御力和表示形式依赖状态机已经处理的变更消息及 Type6／7 查询刷新
- 连锁历史包含服务器明确发送的逐项进度与无效通知，仍不是完整的效果处理日志，结束不等于效果成功适用
- 卡片效果文本来自本地 `cards.cdb`，其语言和版本取决于当前数据库
- 卡片效果描述支持旧式与新版 `aux.Stringid`，系统级描述编号仍可能只有原始数值
- 游戏聊天的 `player_type` 可以区分己方、其他玩家、观察者或系统来源，但服务器自定义颜色和扩展身份可能只能归为系统消息
- 效果发动位掩码当前根据 `description_id` 低位映射到八个槽位，只表示状态机已观察到的发动事件，不等同于完整的一回合一次规则判定
- 复杂宏动作候选由规则枚举生成，候选超过 120 个时使用 Core 第一阶段动作概率加权缩减；没有 Core 时使用均匀权重

## 在线查询与信息质量

Link 3.3.0 为服务器查询引入独立的已知字段记录。查询明确发送的攻击力、等级等即使为零也保留；未发送的动态值不会用卡库数值冒充当前状态。由查询产生的卡片条目包含 `dynamic_state_source=server_query`、`query_sequence` 和 `known_dynamic_fields`，无法确认的动态字段保持缺失

3.3.2 进一步传递查询明确提供的阶级、连接值、禁用状态、所有者和逐项指示物。移动、交换、洗牌或表示变化后，受影响卡片的旧查询属性失效，条目标记 `dynamic_state_source=query_invalidated`；旧攻击力等动态值从观察中移除，等待下一次服务器刷新，无关卡片的查询不受影响。指向旧位置的装备和取对象关系同时清理

`information_quality.online_coverage` 会说明是否收到完整 Start、物理座位、刷新序号、查询缓存合同、实际提示接收计数及目前缺口。当前 `online_v4_inference_ready=false`，表示 Link 的 V4 在线历史、状态与编码仍待验收，不是当前公开字段都不可用，也不是 Core 必须另建 Link 服务

旧事件路径仍有依赖静态数据的字段，因此不能把整个观察当成完整引擎查询快照。公开提示、完整效果处理历史和规则限制仍按下方缺口理解。这个报告随标准观察提供给本地 LLM 和远程智能体，不会绕过可见性与会话隔离

详细边界见 [V4 接入说明](V4_ADAPTER.md)

V4 结构诊断和本方响应区间记录不自动附加到 LLM 请求，不增加这部分 token 开销。它们用于维护与对照，不代表本地或远程智能体能够取得对手隐藏信息、每次私有选择或完整引擎内部状态

3.3.4 在原有 `chain` 和 `recent_history` 中补充 `chain_number`、`stage`、`chain_ended`、`resolution_observed`、`activation_negated_observed` 和 `effect_disabled_observed`。`stage` 为 `announced`、`confirmed`、`resolving` 或 `resolved`，只说明收到过对应通知；未收到无效消息不等于效果必然有效。近期记录仍限八项，不增加独立 LLM 请求

未匹配到发动记录的连锁编号只计入 `information_quality.chain_notifications.unmatched_notifications`，不猜卡名或生成虚构历史。公开连锁记录与模型原生连锁、转移历史分开维护，不改变 V4 输入含义。装备和目标事件标记 `relation_state_source=server_event`，可覆盖旧查询中的关系事实；事件本身不会创建缺失实体或公开盖伏卡片身份

## Link 独立公开上下文

从 3.3.1 开始，标准观察的 `public_context` 同时提供给本地 LLM、AstrBot 和 dsh。Type120／160／165 的处理不经过 Core 受控消息源过滤器，也不会因此多发一个决策请求

- `hints`：当前数值提示、卡片说明引用及玩家提示，附玩家、宿主位置、原始值、引用数和观察回合；有可靠公开身份时补充说明文本
- `known_deck_positions`：仅包含明确公开的位置和卡片，不读取查询中的隐藏主卡组顺序
- `recent_notifications`：近期错过发动时点通知，包含实际观察到的卡片及回合，不猜测失败原因
- `deck_reversed`、`grave_access_restricted` 和 `tracked_hint_lifecycle_consistent`：已跟踪的公开状态与可信边界

提示存在不等于效果一定生效。数值提示与说明引用分别维护，撤销按次数处理；移动、洗牌、空槽和换卡会迁移或清理旧实例信息。玩家级查看限制由明确的提示通知控制，区域重建不冒充撤销

隐藏宿主不会借提示查询卡名或关联卡密。对手已知抽卡只进入无序记忆，不恢复手牌槽位。观察只允许实际收到该消息的玩家视角，单方信息不会通过切换观察者变成另一方的事实

每份观察最多列出 128 条当前提示、512 条公开位置和 16 条近期通知；提示超过上限时 `hints_truncated=true`。这些上限用于约束上下文，不能据此宣称模型全部输入已经齐全。Type35 尚未完成，Type162 在 3.3.16 已接通占位与数量恢复；重建不包含完整身份和提示，因此生命周期仍标为不完整，完整 V4 历史另行实现

Type162 重建后的卡片标记 `dynamic_state_source=reload_field`，未提供的动态攻防等属性不会冒充已知值，后续服务器查询可以补齐。当前连锁的 `stage=snapshot` 只表示它出现在快照中，不表示已看到发动和结算过程。`information_quality.online_coverage` 提供重建次数，以及带序号的最后一次快照规则号和双方正面额外数量；这些数量是该快照时点的信息，不是之后所有时点的实时保证

## 当前尚未完整提供的信息

- 部分系统级 `description_id` 对应的本地化文本
- 不通过装备或取对象消息表达的其他卡片关系
- 未由查询明确提供的无效状态，以及需要推断的效果适用状态
- 持续到回合结束或指定阶段的残留效果
- 当前特殊召唤、攻击和发动限制
- 尚未适配的服务器自定义提示和部分系统字符串
- 完整的伤害步骤、连锁结算和效果处理历史
- 胜负概率、未知卡概率分布和经过校准的 Core 置信度

## 卡片知识范围

从 3.3.5 开始，数量型提示 15／18／24／140／141 的已知共同约束放在顶层 `selection_context`，包含 `minimum`、`maximum`、`cancelable` 和 `finishable`；只有候选一致且字段存在时才提供，避免对每个选项重复发送。未知约束不补成确定值

3.3.6 对复杂选择直接使用已解析的服务器提示补充共享上下文，保持以下含义：

| 类型 | 上下文字段及含义 |
| --- | --- |
| 15 多选 | 最少／最多张数及是否可取消 |
| 20 祭品 | `minimum_release_value` 为最低解放值，`maximum_cards` 为最多张数，二者不能当成同一种数量 |
| 22 指示物 | `counter_type` 和 `required_counters`，完整候选逐张列出实际 `remove_count`，基础评分候选列的是 `available_counters` |
| 23 求和 | `mode`、`target`、`mandatory_count`；精确和才提供可选张数范围，SumGreater 的 `optional_count_limits_apply=false` |
| 21 连锁排序 | `sort_kind=chain`、排序长度和默认顺序出口，`effect_descriptions_available=false` 明确说明提示未提供效果描述 |
| 25 卡片排序 | `sort_kind=card`、`order_length`，候选的 `selection_items` 按结果顺序列出卡片及 `order`，与发送名次向量互相对应 |
| 26 逐项选撤 | 当前已选张数、可选数量和完成／取消标志，候选另给 `selection_operation` 及操作后的 `result_selected_count` |

`selection_items` 同时提供祭品解放值、指示物分配量或素材的 `sum_options`，双值只表示允许选择的数值，不冒充引擎最终采用了哪一个。它们引用既有实体和卡片目录，不额外调用 LLM，也不重复整副卡组文本。查询刷新保留原提示的菜单类型，外层 `event` 仍准确记录当前收到的是查询消息

从 3.3.8 起，排序候选提供 `sort_operation=custom`，逐项 `prompt_index` 是零起始原提示索引，`order` 是从 1 开始的展示顺序。相同实体出现多次时保留各项索引，不合并为同一个选择。Type21 的 `sort_operation=default` 交给服务器采用默认顺序，不表示取消效果；排序菜单也不证明实际连锁已经建立或结算。`default_order_available` 表示当前菜单是否提供这个出口，卡片排序仍维持原菜单

只有通过实际字节约束的完整候选才可提交。求和没有取消响应，规则回退也不重发已被服务器拒绝的动作。该保护不等同于全部效果规则已经可见，搜索和候选池仍有边界。V4 从 3.3.9 起可在混合／复核模式提供兼容在线建议，转移历史缺失默认屏蔽，Core 建议仍按现有统一接口附加

合法菜单的表示选项提供 `position_id`／`position_name`，种族属性宣言提供 `announcement_mask`，数字宣言提供 `announced_number`。这些值不会被误作效果描述编号查询。数字本身与回复服务器的选项编号不同，LLM 仍只返回 `choice_id`，由 Link 打包响应。Type12 的十三字节读取修正后，效果描述和效果槽使用正确编号

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

Type16 连锁菜单区分各候选的 `effect_mode`／`forced`、整体 `cancelable`、特殊候选数和双方提示时机。特殊候选数不表示强制发动，任一候选带强制标志时不提供取消选项；卡号和效果描述按完整候选读取，不从尾部残字节构造其他指令。旧版布局将全局强制标志映射到每个候选，未提供的特殊候选数按兼容零值处理

当前输入已经覆盖基础动作语义、公开素材、常见卡片关系、可缓存卡组知识、受控自主介入和有界游戏聊天历史，但仍不足以保证复杂竞技决策始终有效

QQ 等平台对话不会进入 Link 的 LLM 上下文，由 AstrBot 插件独立管理。游戏聊天目前只随动作决策调用 LLM，对手发言本身不会触发独立模型请求；若需要无动作时点的即时自主闲聊，应增加与决策队列分离的低优先级聊天调度器
