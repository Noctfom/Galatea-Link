# Core 可选增强：ONNX 状态终局输出

本说明对应 Link 3.4.0，参考只读 Core 提交 `2bd44a170e68c64630c7edcbae7f14eb5da7c4a0`。没有修改 Core 源码。本次没有发现新的 Core 解析缺陷，以下属于可选的部署输出增强

## 当前已有能力

Core 的 V4 `GalateaNet.forward(..., return_evaluation=True)` 已能在同一次前向中取得状态级终局预测，`evaluate_state()` 已能跳过动作塔。目标来自 `goal_planner.target_decoder.evaluate_terminal(plan_latent)`，不是依赖所选动作的训练辅助头

普通 ONNX 的 `model_artifacts.py` 导出包装器和导出验收目前只提供 `action_logits`、`values`。因此 Link 的现有 ONNX 模型可以显示价值，但没有足够信息恢复独立胜／平／负概率或剩余事件数

## 建议的可选导出合同

保留现有双输出导出与旧模型加载，另加明确的终局输出选项。启用时包装器调用 `return_evaluation=True`，返回下列四项：

| 输出名 | 形状 | 含义 |
| --- | --- | --- |
| `action_logits` | `[batch, MAX_ACTIONS]` | 既有策略输出，语义不变 |
| `values` | `[batch, 1]` | 既有折扣价值，语义不变 |
| `terminal_outcome_logits` | `[batch, 3]` | 顺序 loss、draw、win 的原始 logits |
| `terminal_remaining` | `[batch]` | 当前既有归一化对数长度，不先转换成回合数或概率 |

剩余长度由消费端按 `expm1(max(0, value) * log1p(1500))` 还原。若未来改变归一化常量、类别顺序或长度单位，应显式增加预测输出合同标记；不要只保持输出名而改变含义

网络权重、108 项 V4 输入、模型协议和结构修订保持原合同。制品清单可记录可选输出集合或 `galatea.state_terminal.v1` 标识，导出校验同步支持双／四输出，不应只改包装器就让后续验收仍要求恰好两个输出

## 建议验收

- 同一权重、同一输入的双／四输出图，策略和价值保持数值一致
- 终局 logits 与剩余长度同 PTH 的状态级输出一致，覆盖双方视角及有限数值
- 无动作候选场面不伪造菜单，价值与终局头不依赖采样出的操作
- 旧双输出 ONNX 继续加载，缺少可选输出时只报告不可用，不影响对局
- 胜／平／负预测标注未校准，不用折扣价值代替胜率，也不把双方独立可见状态的预测强制互补

Link 已实现可选输出读取和受控四输出图测试。旧图仍使用原双输出调用。本说明不要求为了继续对局重导出旧图；只有需要 ONNX 概率／长度面板时再选择增强导出
