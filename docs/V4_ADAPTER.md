# V4 接入进度与验证

Link 3.3.0 开始提供隔离的模型协议 V4 适配器，3.3.1 接通 Link 独立公开提示与新版语义兼容清单。V4 在线决策仍等待 Link 自身的动态状态、历史和编码对照完成，V3 继续作为在线稳定接入路径

Core 维护模型合同，Link 维护真实服务器解析。受控 Core 接入可复用已有接口，无需另建完整服务转换层，职责见 [V4 模型合同与 Link 对接职责](CORE_V4_ONLINE_CONTRACT.md)

## 当前能力

| 项目 | 状态 |
| --- | --- |
| 安全识别 V4 PTH、ONNX 和制品清单 | 已完成 |
| GKG4 导入、模型选择、独立资产目录 | 已完成 |
| 完整 V4 离线快照编码与前向推理 | 已完成 |
| ONNX 编码及运行时不依赖 Torch | 已验证 |
| 在线 Start、Type6/7 查询及动态字段传导 | 已接入，仍需真实服务器回归 |
| Type120／160／165 和提示生命周期 | Link 独立接入，待真实服务器回归 |
| 在线事件历史和模型编码对照 | Link 后续阶段 |
| V4 在线自动决策 | 暂未开放 |
| V3 在线接入 | 保留兼容 |

模型状态中的 `offline_inference_ready=true` 只代表已加载后端可以处理完整 V4 快照，不代表当前服务器观测足够完整。V4 同时公开 `online_inference_ready=false` 和 `online_block_reason`

在 `core_only`、`hybrid` 或 `llm_review` 模式选中已加载的 V4 模型，Link 会在连接服务器前说明原因并拒绝启动。显式选择 `llm_only` 可以继续在线使用本地或远程智能体，V4 模型不会参与建议。Link 不会自动更改决策模式

## 协议身份

本次适配来源为 Core 提交 `1b0fa025994b6eabb8c58dc1ed2090bfbea28d04`，当时框架版本为 3.15.8

3.3.1 另行参考提交 `f83f700e1624dda70531d6bdb51b5fd40e3d4916` 的语义兼容规则，并核对当前 3.16.3 工作区，网络与输入结构未变，不因框架版本新增另一套 V4 适配器

| 身份 | 当前值 | 用途 |
| --- | ---: | --- |
| `model_protocol_version` | 4 | 选择模型适配器 |
| `protocol_schema_revision` | 14 | 固定输入字段、形状与语义 |
| `checkpoint_format_version` | 3 | 读取 PTH 容器 |
| `artifact_manifest_version` | 3 | 读取同轮制品清单 |
| `package_format_version` | 4 | 导入 GKG 部署包 |

另行校验结构哈希、词表容量、词表前缀哈希、语义格式、语义前缀哈希与卡片数量。结构修订不同的早期 V4 开发权重会拒绝加载，即使都声明模型协议 4

框架软件版本和服务器 TCP 握手版本不参与选择模型结构。升级模型也不会自动改变在线 Type16、Type31 等消息布局

## 资产与导入

V4 必需文件为：

- `card_vocab.json`：从保留 token 后开始连续编号的精确词表
- `semantic_lookup_v1.npz`：已经编译的数值语义表
- `semantic_lookup_v1.json`：语义身份、效果槽和提示来源等轻量目录

`cards.cdb`、`meta_staples.json` 和包内源语义资产可一并导入。编译语义是推理必需输入，Link 不会因为缺少文件而自动重建或退回 V3 资产。V4 必需校验也不受 `strict_asset_hashes=false` 绕过

在 WebUI 的模型仓库导入 Core 生成的 GKG4，再选择模型即可保存路径。模型位于 `models/`，资产位于 `model_assets/v4/<model_id>/`；V3 继续使用 `model_assets/v3/<model_id>/`。卡密 token 继续只追加，语义内容更新按下方合同校验

PTH 使用安全权重加载并严格匹配网络参数。ONNX 要求同轮 `.artifacts.json`、主图和声明的全部外置权重；图内模型身份与制品身份必须一致。创建 ONNX Runtime 会话前检查外置引用、文件名和字节范围

V4 模型的资产页会说明需导入匹配的 GKG4，并禁用 V3 卡库及语义重建入口，避免旧维护器写坏当前模型资产。142 兜底池仍可独立调整

### 语义内容更新

新版 GKG4 可以携带 `semantic_compatibility_v1.json`。Link 合并同模型池已有的历史证据并检查冲突，原始部署包和权重文件保持原样

| 制品 | 内容变化后的处理 |
| --- | --- |
| PTH | 核验原始卡密前缀、标签编号前缀、编码器、分块策略和向量维度后，允许使用新语义表 |
| ONNX | 图内已经冻结语义表，仍要求导出时的语义前缀，不能把内容更新等同于普通词表追加 |

缺少兼容证据或编号、坐标系变化会拒绝加载。同一池有旧 ONNX 使用原表时，也会在安装模型前拒绝破坏该图的资产替换。需要保留旧池资产，或在 Core 重新导出匹配制品

兼容更新后的运行状态保留制品原身份，另提供 `runtime_asset_identity` 和 `semantic_update`，控制台也会提示预测可能随新知识变化。这不是跳过哈希检查或修改模型结构

## 离线验证

只读制品目录即可验证，不需要将 Core 加入 `PYTHONPATH`。将下面路径换成自己的模型和资产位置

```powershell
python scripts/test_model_protocols.py path/to/galatea_iter_210.pth --assets path/to/core_assets --full
python scripts/test_model_protocols.py path/to/galatea_iter_210.onnx --assets path/to/core_assets --full
```

ONNX 使用 `requirements-onnx.txt`，包括 ONNX 图检查库和 ONNX Runtime，不安装 Torch。PTH 使用 `requirements-pytorch.txt`

脚本构建的是人为指定的离线测试快照，仅用于核对加载、字段编码和前向运算。它不能证明服务器能够提供全部在线输入。真正的在线接入仍经过单独的玩家观测检查

3.3.0 在相同语义资产下对第 210 轮 V4 PTH／ONNX 做了 CPU 离线对照：最小测试快照的合法动作 logits 最大差约 `1.34e-7`，value 差约 `2.98e-8`。3.3.1 已用该 PTH 与 Core 当前更新后的资产验证兼容加载和前向推理；更新后的 PTH 不应再要求与冻结旧语义的 ONNX 数值一致。这些结果不代表策略质量或真实在线覆盖率

## 在线解析改动

`core/online_state.py` 与模型编码器独立维护服务器公开状态，目前提供以下基础能力：

- Start 精确读取 18 字节载荷，固定玩家物理座位及双方 LP、主卡组和额外数量
- Type6 整区刷新、Type7 单卡刷新先验证整包再提交，整区刷新会清除空槽
- Type6 按兼容服务器的独立 `STOC_GAME_MSG` 载荷解析；其他封包布局需要单独的报文配置和样本
- 保留实际发送的字段集合和查询序号，动态等级、攻击力等已知零不再被数据库默认值覆盖
- LLM 观察只输出查询已知的动态值，未提供的值保持缺失；身份静态资料仍由卡库提供
- 查询不会生成新动作，也不会让正在等待的后台决策重新排队
- 对手隐藏信息和主卡组查询身份不会进入公开投影
- 未确认缓存合同时默认保守处理，不从字节猜测增量合并；`replace`、`merge` 是内部适配合同，尚未开放任意用户切换
- Type120、160、165 由 Link 独立处理，不沿用 Core 受控消息源的过滤器，也不因为收到提示就额外排队决策
- 维护卡片数值提示、描述引用、玩家提示、公开顶卡、卡组反转、抽卡与洗牌失效；空槽和明确换卡移除旧实例提示
- 提示与记牌按实际消息接收者绑定，隐藏宿主不会借提示泄漏身份；玩家查看限制由明确通知添加或撤销
- 分开记录回合玩家、先手玩家和当前决策者，LLM 观察新增 `public_context`

观察增加 `information_quality.online_coverage`，报告固定座位、Start 是否收到、查询合同是否确认和当前限制。它会明确保留 `online_v4_inference_ready=false`，不是供调用者修改的完整性声明

生产消息拆分遇到未知类型或截断会记录解析失败，取消旧决策、清空旧动作、结束本局连接并记录 `protocol_error`，避免继续消费错位字节。当前只保留原有在线 Type16／Type31 布局，没有照搬 Core 原生布局或重新猜测幽灵字节

## 尚待完成

1. Link 依据现有 V4 输入规格继续补齐动态状态、动作和在线历史，若出现输入语义缺口，再向 Core 提供具体字段和复现样本
2. 据真实服务器样本建立独立的消息布局配置，继续核对 Type16、31 等差异；Type162 重建和特殊玩法单独处理
3. 构建 V4 在线快照并逐字段对照，不以 Core 诊断入口的固定拒绝状态替代 Link 自己的就绪判断
4. 完成双方座位、复杂选择、提示失效、失败取消和超时的真实在线回归，再开放对应来源的 V4 在线推理

BO3 换备、在线学习、搜索和新的策略评估接口不属于这一批

## 维护与回归

`model_protocols/v4/source_manifest.json` 记录只读来源提交及标准化 UTF-8 源文本摘要。`scripts/sync_core_v4_runtime.py` 是开发辅助工具，只读取固定来源并打印补丁，不在运行时导入 Core，也不会修改 Core。生成内容仍须审查，不能覆盖手写包装或绕过结构身份检查

完整开发回归：

```powershell
python -m pip install -r requirements-test.txt
python -m pytest tests -q
```

可选设置 `GALATEA_CORE_READONLY_ROOT` 指向 Core 目录，启用原始编码器逐字段对照。测试只在内存中编译该编码器，并把依赖指向 Link 的隔离协议实现；不会写入 Core 或生成 Core 字节码文件
