# Core 解析只读核对记录 · 2026-11-05

本日分别针对 Link 3.3.14 的 Type31 和 3.3.15 的 Type16 在线崩溃核对 Core，读取的 Core 提交为 `2bd44a170e68c64630c7edcbae7f14eb5da7c4a0`，未修改 Core 文件，也未将 Core 作为 Link 运行依赖

## 本轮 Core 结论

**未发现 Core 在 Type31 或 Type16 上的同类解析错误，本轮无需为此修改 Core**

- `core_message_protocol.py` 的 `read_confirm_cards` 读取 `player、skip_panel、count` 三字节头，再读取七字节卡片记录
- `gamestate.py` 的动态长度计算通过该读取器计算 Type 31 的实际长度
- 状态更新通过 `parse_confirm_cards` 读取确认卡片；`skip_panel` 只控制展示面板，不改变消息接收者已获知的卡片身份
- 测试在内存中提取 Core 解析函数和长度函数，分别验证面板标志为零和一的载荷、读取消费长度及流位置复原；没有在 Core 目录生成测试文件或模块缓存

核对范围包括 Type31、Type16 及相关受控输入，以及 3.3.16 追加的 Type162 字段读取，不表示已验收 Core 的全部消息类型

## Link 3.3.14 的 Type31 问题

Link 将 Type 31 按 `player、count` 两字节头解析，漏掉了真实网络协议的 `skip_panel` 字段。以单张卡片为例，完整网络帧应为 `1 + 3 + 7 = 11` 字节。`skip_panel=0` 被误当成零张卡片后，真实数量一会被读成 Retry，后面的卡号低字节又被读成其他指令

这不是需要整体开启本地缓冲区定界处理的问题，不能用全局“幽灵字节”开关区分。Link 已将 Type 31 独立为网络确认协议，支持有面板标志的消息及旧版两字节头，三处消费共用同一解析结果

对应的公开实现可参考 [YGOPro 服务端 Type 31 转发](https://github.com/Fluorohydride/ygopro/blob/master/gframe/single_duel.cpp) 和 [EDOPro 兼容模式的面板标志读取](https://github.com/edo9300/edopro/blob/master/gframe/duelclient.cpp)

Link 同时补充展示信息到 LLM 卡片目录与近期通知。衍生物尚未进场时只登记已展示的身份，不提前生成场上实体；这属于 Link 上层信息传导，不是 Core 解析缺陷

## 回归入口

`tests/test_confirm_cards_protocol.py` 覆盖十一字节复现、双方座位、面板开关、旧版头部、合并消息、公开卡组位置记忆及只读 Core 对照。`tests/test_v4_live_inference.py` 在实际 TCP 流程中插入确认消息，验证 PTH／ONNX 可以继续响应后续操作

## Link 3.3.15 的 Type16 问题

用户第二次连接房间后在开局收到二十六字节 Type16 报文，Link 将单候选的十四字节误读成十三字节，留下的描述末字节 `0x16` 被当成 Type22，报错位置为 `offset=25`。这属于连锁候选布局遗漏；日志前一局的胜负结束和重连不是该长度错误的依据

当前 [YGOPro 服务端转发](https://github.com/Fluorohydride/ygopro/blob/master/gframe/single_duel.cpp) 使用十一字节头和每候选十四字节，[客户端读取](https://github.com/Fluorohydride/ygopro/blob/master/gframe/duelclient.cpp) 区分以下字段：

| 部分 | 字段 |
| --- | --- |
| 十一字节头 | `player:u8, count:u8, special_count:u8, hint_timing:u32, opponent_hint_timing:u32` |
| 十四字节候选 | `effect_mode:u8, forced:u8, code:u32, location:u32, desc:u32` |

`special_count` 不是全局强制标志，是否能取消取决于全部候选的 `forced`。Link 此前同时存在长度、卡片字段和取消权限错位；规则回退还曾把提示时机的低字节当成强制标志，现均共用 `core/chain_protocol.py` 解码。旧版十一字节头、十三字节候选、全局强制标志的布局继续兼容

本批使用人工构造的二十六字节帧及末字节 `0x16` 复现完全相同的错误位置，不声称掌握用户完整现场载荷。回归还覆盖双方座位、空／多候选、每候选不同强制标志、旧布局、后续合并消息及真正的列表截断

### Core 单独核对结论

- `core_message_protocol.py` 的 `read_chain_prompt` 已按 `<BBBII` 读取头部，逐项按 `<BBIII` 读取十四字节候选
- `parse_chain_prompt` 的全局 `forced` 是候选强制标志的逻辑或，未把 `special_count` 当作强制标志
- `gamestate.py` 的动态长度和动作分支已调用上述读取器，模式、强制位、卡号、位置、描述及提示时机均正确
- 通过内存 AST 只读提取字段读取、长度计算、动作和快照函数，双方座位及强制／非强制场景下，Link 投影的全部一百零八项 V4 输入与 Core 一致
- 对照发现 Link 曾给原生未设置响应特征的连锁取消补写负一，现已在 Link 在线映射中修正；发包仍使用索引负一。这是 Link 模型输入适配差异，不是 Core 解析缺陷

回归入口为 `tests/test_chain_prompt_protocol.py`。`tests/test_v4_live_inference.py` 另以实际 PTH／ONNX TCP 链路覆盖直接从非空连锁开局，以及之后强制连锁与排序的连续三次模型决策，禁止规则回退，确认消息拆分后可继续运行。真实生产权重和用户现场第二次开局仍需现场复核

## 3.3.16 追加：Type162 字段核对

Core 当前 `read_reload_field` 已固定读取七怪兽槽、八魔陷槽，与本仓库保存的 YGOPro `reload_field_info`／`query_field_info` 一致。本次没有发现 Core 的同类长度或字段错误，不需要为此修改 Core

Link 此前仍按规则号在五／七怪兽槽间选择，且状态层没有真正恢复 Type162 的基本分、区域占位和连锁。这是 Link 的适配缺口，现已独立修复。只读 AST 提取的 Core 解析结果与 Link 的正常七／八槽样本一致，测试入口为 `tests/test_reload_field_protocol.py`

本次对照仅验证 Core 快照字段读取，不把 Link 自身的玩家级提示保留、异步任务取消和网络会话行为作为 Core 的缺陷判据。快照不含身份与历史时如何保留来源，应按各项目用途分别处理。PTH／ONNX TCP 回归已在连锁与排序之间增加重建和后续身份查询；真实服务器触发流程仍待现场覆盖
