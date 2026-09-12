# Core 模型协议适配

Link 将检查点容器格式、模型输入协议和具体 Core 发布版分开识别，避免仅靠文件名或框架版本猜测网络结构

| 适配器 | 已知 Core 发布版 | 检查点格式 | 模型协议 | 动作上限 | 状态 |
| --- | --- | ---: | ---: | ---: | --- |
| `core-3.4.2-model-v1` | 3.4.2 | 1 | 1（旧检查点隐式值） | 80 | 冻结兼容 |
| `galatea-model-protocol-v3` | 3.6.3 至 3.6.5 | 2 | 3 | 120 | 当前稳定协议 |

当前新增功能、ONNX 推理和外部接口以模型协议 V3 为正式目标。V1 代码仅冻结保留，不要求新功能回迁，也不作为能力接口的稳定协议声明

`model.protocol: auto` 会先安全读取检查点元数据，再选择唯一匹配的适配器。推荐使用 `auto` 或 `v3`，历史固定值 `core-3.6.3` 和 `core-3.6.5` 都只作为 V3 别名保留。固定值与制品真实协议不一致时会直接拒绝加载，未知格式或协议不会落入某个看起来相近的网络

## 模型协议 V3 适配范围

- 独立保存 V3 特征编码器、网络结构、语义资产校验和效果槽绑定逻辑
- 支持 120 个动作、五个目标槽、显式操作与响应、选择约束、动作签名和九维连锁上下文
- Link 的动作快照补齐 V3 字段，同时旧 V1 编码器继续忽略新增字段
- 运行时状态公开 `adapter_id`、检查点格式、模型协议、模型 UUID 和迭代编号
- 默认交叉校验知识库、代码语义向量、索引和运行时效果槽，防止不一致资产与权重静默混用
- 同一 V3 适配器可选择 PyTorch 检查点或 ONNX Runtime 图，外部动作、LLM 和 AstrBot 接口不随推理后端变化
- ONNX 加载会校验 `.artifacts.json`、主图、外置 `.onnx.data`、模型 UUID、迭代号和图内身份元数据
- V3 编码器可直接生成 NumPy 输入，ONNX 路径不会导入 Torch

Link 不会在运行时复制、导入或修改 Galatea Core 的源码。`model_protocols/v3` 表达的是稳定模型协议，而不是某个 Core 发布快照。Core 只要继续产生协议 V3、检查点格式 2 和部署包格式 2，Link 就复用同一个适配器；只有 `model_protocol_version` 改变时才新增隔离目录

## YGOPro 在线协议边界

模型协议升级不改变 Link 的在线网络输入边界。YGOPro 在线消息继续由 `core/gamestate.py` 以 `CORE_HAS_GHOST_BYTE = False` 解析；连锁选择头也继续采用在线协议中不含本地 Core `Spe` 字段的布局

因此 Core 本地缓冲区协议和模型输入协议是两层独立兼容点，不应通过复制整个 Core 消息解析器来混合

## 直接验证只读 Core 目录

以下命令只读 Core，不会复制或修改 Core 文件

```powershell
python scripts/test_model_protocols.py `
  path/to/galatea_model.pth `
  --assets path/to/core_assets --full
```

对应配置为

```yaml
model:
  weights_path: "E:/Galatea_Core/models/galatea_iter_100.pth"
  expected_model_id: "5381b6f8-b492-43b8-9f16-6957920826fa"
  protocol: "v3"
  assets_path: "E:/Galatea_Core"
  strict_asset_hashes: true
```

使用同轮 ONNX 制品时配置为

```yaml
model:
  weights_path: "E:/Galatea_Core/models/galatea_iter_100.onnx"
  expected_model_id: "5381b6f8-b492-43b8-9f16-6957920826fa"
  protocol: "v3"
  inference_backend: "onnxruntime"
  onnx_providers: ["CPUExecutionProvider"]
  onnx_intra_op_threads: 0
  assets_path: "E:/Galatea_Core"
  strict_asset_hashes: true
```

`.onnx`、`.onnx.data` 和同轮 `.artifacts.json` 必须位于同一目录。ONNX 图要求的输入类型高于编码器内部存储类型时，运行时会按图签名转换

## GKG 导入和模型选择

推荐在 Link WebUI 的“模型仓库”页面导入 Core 生成的部署包格式 2 `.gkg`。Link 会限制压缩包路径、成员类型、数量、大小与压缩比，核对部署清单和制品身份，再把模型写入 `models`，把语义资产写入 `model_assets/v3/<model_id>`。不同模型 UUID 的资产不会互相覆盖

较大的包可以先放入忽略版本控制的 `deploy_packages`，再从页面选择本地导入，避免浏览器重复传输。会话运行中选择模型不会替换内存里的后端，而是在下一次启动 Link 时生效

也可以从只读 Core 目录直接整理模型

```powershell
python scripts/import_core_v3_bundle.py `
  --core-root path/to/Galatea_Core `
  --iteration 100 `
  --format onnxruntime
```

历史入口 `scripts/import_core_363_bundle.py` 仍可使用，但只转发到通用 V3 脚本，不再检查或写死 Core 发布版本

## 后续版本扩展规则

1. 先为新检查点分配明确的 `model_protocol_version`
2. 在 `model_protocols` 下新增隔离目录和适配器标识
3. 将对应网络、编码器和资产校验固定在新目录
4. 在注册表中加入精确匹配，不修改旧适配器语义
5. 分别运行旧版回归、检查点探测、完整前向推理和真实 YGOPro 在线对局
