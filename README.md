<div align="center">

<img src="docs/logo.png" alt="Galatea Link Logo" width="50%">

# Galatea Link

![版本](https://img.shields.io/badge/Link-3.0.0-6b8f71)

</div>

Galatea Link 是一个独立的游戏王智能体运行与接入层。它通过 YGOPro 兼容协议连接游戏服务器，并把 Galatea Core 模型、本地 LLM、AstrBot 主智能体、WebUI 与外部 API 统一到同一套异步对局流程中

项目源自 [Galatea Core](https://github.com/Noctfom/Galatea-Core/tree/main) 的在线接入需求，但运行时与 Core 仓库完全独立。Link 项目可以独立运行；模型和运行资产可通过稳定的模型协议或 GKG 部署包导入

> 当前处于开发阶段，优先支持模型协议 V3 和 YGOPro 兼容服务器。请先在测试房间验证卡组、协议版本和决策时限，再用于正式对局

## 已实现能力

- 连接本地或在线 YGOPro 兼容服务器，支持服务端协议版本拒绝后的有限自动重试
- 加入房间、上传卡组、准备、猜拳、先后攻选择，以及房主在双方准备后的自动开局
- 加载模型协议 V3 的 PyTorch 或 ONNX 模型，支持 GKG 格式 2 导入和模型资产校验
- `core_only`、`hybrid`、`llm_review`、`llm_only` 四种决策模式
- Link 内置 OpenAI Compatible LLM，支持结构化动作、静态前缀缓存、请求诊断和安全回退
- AstrBot 作为主智能体远程决策、游戏内聊天、查卡、卡组导入及会话隔离的临时卡组编辑
- 玩家可见观察、远程合法动作提交、运行时控制、WebSocket 事件流和低开销赛后摘要
- 浅色/深色 WebUI，可配置连接、模型、LLM、决策策略、运行资产和游戏聊天
- Link 本地卡组仓库，可导入、查看、选择、单卡调整和安全删除 YDK
- 卡库、Lua 脚本、语义资产同步，以及 MSG 142 宣言兜底池管理

## 组件关系

```text
YGOPro 兼容服务器
        ↕ TCP
Galatea Link
  ├─ Core 模型：PyTorch / ONNX Runtime
  ├─ 本地智能体：OpenAI Compatible LLM / RuleBot
  ├─ 控制面：WebUI / HTTP API / WebSocket
  └─ 远程智能体：AstrBot 插件 → QQ 等平台
```

Link 始终负责游戏协议、玩家可见状态、合法动作校验和超时回退。AstrBot 接管时，AstrBot 的当前人格与模型负责决策和交流，但不需要安装 Torch 或 ONNX Runtime

## 快速开始

推荐使用 Python 3.11

### 1. 创建本机配置

PowerShell：

```powershell
Copy-Item config.example.yaml config.yaml
```

Linux 或 macOS：

```bash
cp config.example.yaml config.yaml
```

`config.yaml`、`link_state.json`、`link_secrets.json` 和 `.env*` 均被版本控制忽略。完整字段说明见 [配置参考](docs/CONFIGURATION.md)

### 2. 安装依赖

仅使用 LLM、RuleBot 和服务接口：

```bash
python -m pip install -r requirements-base.txt
```

推荐的 ONNX 部署：

```bash
python -m pip install -r requirements-onnx.txt
```

PyTorch 模型部署：

```bash
python -m pip install -r requirements-pytorch.txt
```

完整开发与本机语义构建环境：

```bash
python -m pip install -r requirements.txt
```

### 3. 准备卡组与模型

- 在 WebUI 的“卡组仓库”导入至少一副 `.ydk`，或手动放入 `decks/`，并让 `agent.deck` 与文件名对应
- 将 PTH/ONNX 模型放入 `models/`，或启动服务后从 WebUI 导入 GKG
- 模型协议 V3 的运行资产放入 `model_assets/v3/<model_id>/`，推荐随 GKG 一并导入
- 只运行 `llm_only` 时可以没有 Core 模型，但仍然需要合法卡组

### 4. 配置密钥

推荐通过进程环境传入密钥，不要写进 YAML：

```powershell
$env:GALATEA_LLM_API_KEY = "替换为自己的 LLM API Key"
$env:GALATEA_LINK_API_TOKEN = "替换为足够长的随机访问令牌"
```

如果服务仅监听 `127.0.0.1`，Link API Token 可以暂时留空。监听 `0.0.0.0` 或允许容器、局域网访问时必须设置 Token

LLM API Key 也可以在 Link WebUI 保存到本机 `link_secrets.json`。页面只显示是否已配置，不能读取或回显明文

### 5. 启动 Link

```bash
python scripts/run_link_service.py --config config.yaml
```

默认 WebUI 地址为 `http://127.0.0.1:8765`

服务启动本身不会加载模型。点击 WebUI 的“启动 Link”或调用会话启动接口后，才会创建游戏连接并加载所选模型

## 决策模式

| 模式 | 行为 |
| --- | --- |
| `core_only` | 优先完全由 Core 模型决策，模型不可用时进入 RuleBot 回退 |
| `hybrid` | Core 高置信度时直接执行，否则交给 LLM 或 AstrBot |
| `llm_review` | 每个可交互时点都让 LLM/AstrBot 参考 Core 建议后复核 |
| `llm_only` | 不依赖 Core 建议，由 LLM/AstrBot 决策，超时后由 RuleBot 保底 |

`decision.agent_backend` 决定 LLM 类决策由谁执行：

- `local`：Link 直接调用 `llm` 段配置的 API
- `remote_astrbot`：Link 发布玩家可见观察，AstrBot 主智能体调用动作提交工具

三类超时彼此独立：

| 设置 | 控制范围 |
| --- | --- |
| `decision.llm_time_budget` | 游戏单个动作等待本地 LLM 或 AstrBot 的总时间 |
| `llm.timeout` | Link 直连 LLM 供应商的单次 HTTP 请求时间 |
| AstrBot `request_timeout` | AstrBot 插件访问 Link HTTP 接口的时间 |

远程决策是否能赶上游戏时点，最终取决于 `decision.llm_time_budget`

## AstrBot 接入

AstrBot 可以运行在另一套 Python 环境或 Docker 容器中，只需填入 Link 的 HTTP/WebSocket 端口地址即可自动连接，不需要复制 Link 的模型依赖

插件启用后，由 AstrBot 主智能体调用工具选择服务器、房间密码和当前会话可见卡组，再启动决斗。游戏内聊天、动作决策和 QQ 对话使用同一个 AstrBot 人格，但由独立异步任务调度

安装、Docker 网络、Pages 设置、工具清单、会话隔离和故障排查见 [AstrBot 远程桥接说明](docs/ASTRBOT_BRIDGE.md)

## 配置与持久化

Link 使用三类本地文件：

| 文件 | 内容 | 是否应提交 |
| --- | --- | --- |
| `config.example.yaml` | 无密钥的公开模板 | 是 |
| `config.yaml` | 本机基础配置，可含服务器密码 | 否 |
| `link_state.json` | WebUI 保存的非敏感覆盖和 revision | 否 |
| `link_secrets.json` | WebUI 保存的 LLM API Key | 否 |

WebUI 保存的 `link_state.json` 会覆盖 `config.yaml` 中对应的服务器、Agent、模型、LLM 非敏感字段和决策设置。因此修改 YAML 后发现数值没有变化时，应同时检查 WebUI 当前值或移走旧状态文件

更完整的安全边界和上传检查见 [安全与隐私](docs/SECURITY.md)

## 验证

运行全部自动测试：

```bash
python -m unittest discover -s tests -p "test_*.py"
```

验证 LLM API、结构化输出和缓存命中：

```bash
python scripts/test_llm_api.py --repeat 3 --interval 2
```

验证模型协议制品：

```bash
python scripts/test_model_protocols.py models/galatea_model.onnx --assets model_assets/v3 --full
```

上传前执行不回显密钥明文的隐私审计：

```bash
python scripts/audit_release_privacy.py
```

如果脚本报告历史提交命中，应先轮换对应凭据，再决定是否清理 Git 历史

涉及在线服务器、房主自动开局、实际超时和胜负原因的行为仍建议在真实测试房间人工验证

## 文档

- [配置参考](docs/CONFIGURATION.md)：所有 YAML 分区、优先级和推荐值
- [Link 独立服务与 WebUI](docs/LINK_SERVICE.md)：服务边界、HTTP API、Docker 和运行资产
- [异步运行时接口](docs/RUNTIME_API.md)：Python API、事件、观察和远程动作提交
- [LLM 对局信息契约](docs/LLM_INFORMATION.md)：LLM 能看到什么、缓存和信息边界
- [AstrBot 远程桥接](docs/ASTRBOT_BRIDGE.md)：插件安装、Pages、工具和会话隔离
- [模型协议适配](docs/MODEL_PROTOCOL_ADAPTERS.md)：V3、PTH/ONNX、GKG 和后续版本扩展
- [安全与隐私](docs/SECURITY.md)：密钥、卡组、日志与 Git 上传检查
- [更新记录](CHANGELOG.md)：从 Link 3.0.0 开始的软件版本记录
- [Core 3.4.2 历史适配](docs/CORE_342_ADAPTER.md)：冻结的旧协议说明，不是当前推荐部署

## 当前限制

- 当前主线模型协议为 V3；新模型协议会新增隔离适配器，不会靠近似形状强行加载
- 在线 BO3 局间换备尚未接入稳定上层状态机，当前只支持赛前编辑临时卡组的副卡组区域
- LLM/AstrBot 可能超过游戏动作时限，Link 会取消过期请求并使用 Core 或 RuleBot 安全回退
- 游戏协议只提供玩家可见数据；Link 不会把不可见卡片或其他会话的卡组信息交给外部智能体
- 上游 Core 仍在演进，Link 会保留稳定版本协议适配层

## 支持与反馈

如有问题或建议，请联系开发者qq：2721298904，加入galateaYGO连接交流群492420925，提交Issue，或加入我的个人闲聊群635990103（快来玩！）

以下项目再次进行感谢：

- [YGOProCore](https://github.com/Fluorohydride/ygopro-core) - YGOPRO 核心引擎，万物之源
- [MDPro3](https://code.moenext.com/sherry_chaos/MDPro3) - MDPro3，目前优先适配的端
- [YGOPro 官方脚本库](https://github.com/Fluorohydride/ygopro-scripts) - 官方 Lua 脚本仓库，卡片效果解析的基础
- [萌卡 MyCard](https://github.com/mycard/ygopro-database) - cards.cdb 卡片数据库来源

以及感谢这两个项目：

- [Duel-galatea](https://github.com/Noctfom/astrbot-plugin-duel-galatea)
- [Galatea-Core](https://github.com/Noctfom/Galatea-Core)

？没错，它们都是我的项目，它们和Link项目一起，构成了GalateaYGO这个项目组

从duel-galatea的想法起点开始，到Core项目的解析模块与认知耕耘，到最后的Link模块回归起点

Link项目的图标是link2妹妹，这里是有小巧思的，Core是link1的产出者，而Link项目则是将模块相互连接的部分

可惜社长造王样没有link怪（）
