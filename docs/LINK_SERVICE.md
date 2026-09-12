# Link 独立服务与可视化界面

`linkd` 是 Link、AstrBot、WebUI 和未来移动端之间的稳定进程边界。游戏连接、Core 模型、ONNX Runtime、LLM 调用、密钥和对局状态全部留在 Link 进程，上层客户端只使用 JSON API 和事件流

## 运行边界

```text
YGOPro 兼容客户端 ── YGOPro 协议 ── Link 服务 ── HTTP/WebSocket ── AstrBot 插件
                              ├──────── 同源 WebUI
                              └──────── 未来移动客户端
```

三类版本彼此独立

- Link 软件版本当前为 `3.0.0`
- 游戏网络层面向 YGOPro 协议，可连接采用兼容协议的客户端与服务端
- 模型协议当前首个稳定版本为 V3，已知 Core 3.6.3 至 3.6.5 产生兼容制品
- 外部接口固定为 `galatea.link.api.v1`

以后新增 V4 模型适配器时，只替换 Link 内部编码和推理层。只要外部能力没有本质增加，AstrBot、WebUI 和移动客户端不需要跟随模型协议升级

## 启动

完整开发环境

```powershell
python -m pip install -r requirements.txt
```

只部署 ONNX Runtime 时

```powershell
python -m pip install -r requirements-onnx.txt
```

仅为现有 Link 安装本机 Lua 语义构建能力时

```powershell
python -m pip install -r requirements-semantic.txt
```

启动服务

```powershell
python scripts/run_link_service.py --config config.yaml
```

服务默认监听 `127.0.0.1:8765`，浏览器打开 `http://127.0.0.1:8765` 即可使用 Link 自带控制台。服务启动阶段不会加载模型，创建对局会话时才加载

## Docker 中的 AstrBot 连接宿主机 Link

Link 配置需要显式开放监听并设置令牌

```yaml
service:
  host: "0.0.0.0"
  port: 8765
  api_token: ""
  api_token_env: "GALATEA_LINK_API_TOKEN"
```

在 Windows Docker Desktop 中，AstrBot 插件地址填写 `http://host.docker.internal:8765`。容器环境和 Link 宿主机环境都设置同一个 `GALATEA_LINK_API_TOKEN`

```yaml
galatea_link:
  enabled: true
  base_url: "http://host.docker.internal:8765"
  api_token: ""
  api_token_env: "GALATEA_LINK_API_TOKEN"
```

非本机监听但没有令牌时，Link 会拒绝启动服务。跨不可信网络时还应放在 VPN 或 HTTPS 反向代理后，不建议把端口直接暴露到公网

## 外部 API

读取接口

- `GET /api/v1/health` 无需鉴权且不会加载模型
- `GET /api/v1/capabilities` 返回 API、V3、YGOPro 和推理后端能力
- `GET /api/v1/status` 返回脱敏会话状态
- `GET /api/v1/controls` 返回宏观控制和 revision
- `GET /api/v1/configuration` 返回脱敏的 YGOPro、Agent、LLM 和服务配置
- `GET /api/v1/models` 返回模型仓库、当前选择和本地 GKG 列表
- `GET /api/v1/assets` 返回当前模型的 CDB、脚本、语义资产和 MSG 142 兜底池状态
- `GET /api/v1/decks` 返回 Link 本地卡组名称、卡片内容和来源
- `GET /api/v1/observation` 返回最近一次 LLM 可见观察
- `GET /api/v1/chat/history?limit=20` 返回游戏内聊天历史

写入接口

- `POST /api/v1/session/start` 懒加载模型并启动 Link
- `POST /api/v1/session/stop` 只停止游戏会话，服务和 WebUI 保持运行
- `PATCH /api/v1/controls` 原子更新介入、Core 温度、自主调整和聊天开关
- `PATCH /api/v1/configuration` 保存下一次会话使用的非敏感配置
- `PUT /api/v1/configuration/llm-api-key` 保存或清除 Link 本机 LLM API Key
- `POST /api/v1/integrations/astrbot/heartbeat` 登记 AstrBot 插件连接状态
- `POST /api/v1/configuration/server/test` 对草稿服务器执行纯 TCP 连通测试
- `POST /api/v1/configuration/llm/test` 对草稿 LLM 执行一次最小结构化输出测试
- `PATCH /api/v1/models/selection` 保存下次会话使用的模型
- `POST /api/v1/models/import` 流式上传并导入 GKG
- `POST /api/v1/models/import-local` 导入 `deploy_packages` 中的 GKG
- `POST /api/v1/assets/card-data/sync` 同步并校验 Link 自有 CDB 与 YGOPro Lua 脚本
- `POST /api/v1/assets/semantics/sync` 同步并交叉校验已发布的完整 V3 语义资产
- `POST /api/v1/assets/semantics/rebuild` 在完整环境中解析 Lua 并增量或全量生成 V3 语义资产
- `PATCH /api/v1/assets/meta-staples` 修改当前模型目录中的 MSG 142 宣言兜底池
- `POST /api/v1/decks/local` 导入或显式覆盖 Link 本地 YDK 卡组
- `PATCH /api/v1/decks/local` 对停止状态下的 Link 本地卡组执行单卡增删或移动
- `DELETE /api/v1/decks/local` 删除未被当前配置选择的 Link 本地卡组
- `POST /api/v1/decks/query` 返回当前 AstrBot 会话可见的隔离卡组
- `POST /api/v1/decks/import` 将当前工具箱缓存 YDK 复制到 Link 会话目录
- `POST /api/v1/decks/copy-local` 将 Link 本地卡组复制为当前 AstrBot 会话的可编辑临时副本
- `PATCH /api/v1/decks/current` 修改当前远程会话已经选中的 AstrBot 临时卡组
- `GET /api/v1/session/configuration` 读取当前仅存于内存的脱敏会话配置
- `POST /api/v1/chat` 将文本发送到游戏内聊天
- `POST /api/v1/external-message` 将 AstrBot 等平台消息投递到当前 Link 事件流
- `GET /api/v1/decisions/pending` 返回当前待处理的远程智能体决策
- `POST /api/v1/decisions/{request_id}` 提交远程智能体选择的合法动作
- `POST /api/v1/session/configure` 保存一次不落盘的远程对局连接、卡组和决策配置
- `POST /api/v1/auth/session` 为同源 WebUI 建立受保护的 WebSocket 鉴权 Cookie

事件接口为 `GET /api/v1/events` WebSocket。事件队列拥塞时只丢弃该客户端最旧事件，不会阻塞游戏收包和决策

除健康和能力接口外，请求使用 `Authorization: Bearer <token>`。响应统一包含 `api_version`，成功结果位于 `data`，错误位于 `error.code` 和 `error.message`

## WebUI 与 Flutter 选择

当前 Link UI 采用服务端自带的浏览器界面，不要求 Node、Flutter 或额外安装，且与 AstrBot 使用完全相同的 API。这个界面目前覆盖启动停止、状态、介入模式、Core 策略与温度、模型导入和选择、本地 YDK 导入与单卡调整、运行资产更新、MSG 142 兜底池、自主调整开关、游戏聊天、LLM 观察和实时事件

策略、模型选择和配置中心不要求游戏会话已经启动。Link 会把这些非敏感覆盖写入忽略版本控制的 `link_state.json`，不会把 LLM 密钥、服务器密码或服务令牌写入该文件。运行中的配置和模型选择在下一次会话启动时生效；TCP 测试只连接并立即关闭，不加入房间

配置中心只允许修改 YGOPro 服务器地址、协议参数、Agent/卡组和 LLM 非敏感参数。协议版本自动协商默认开启，收到 `ERROR_MSG` 类型 4 时会读取服务端返回的目标版本并按上限有限重连，配置版本不会被自动覆写。房间密码可由本机配置或会话临时配置提供，LLM API Key 可由环境变量或 WebUI 的本机密钥存储提供，服务令牌可由本机配置或环境变量提供；接口只返回是否已配置及来源，不返回明文

Link WebUI 可以把 LLM API Key 写入项目根目录的 `link_secrets.json`。该文件已忽略版本控制，不进入 `link_state.json`、GKG、状态接口或事件；WebUI 只能覆盖或清除，不能读取已有明文。环境变量优先级高于本地密钥文件

AstrBot 插件启用自动连接后会定时调用心跳接口。Link 状态中的 `integrations.astrbot` 会列出当前租约内的插件实例，WebUI 概览分别显示 Link 服务、AstrBot 插件和游戏对局三种状态

运行资产按当前模型的 `model_id` 放入 `model_assets/v3/<model_id>`，旧布局的 `model_assets/v3` 作为公共回退。Core 部署格式 2 允许 GKG 只包含运行资产而不包含模型，Link 会把这类纯资产包绑定到当前已选模型；尚未选定模型时才安装到公共回退目录。CDB 也允许作为 Link 扩展资产随包导入，但 Core 当前标准 GKG 不强制包含它

“同步完整语义资产”只下载远端已经生成的 `knowledge_base.json`、代码向量和索引，并在安装前进行完整性校验，不会加载训练依赖。“本机解析 Lua 并生成向量”则复用模型协议 V3 的解析与效果槽绑定规则，支持基于当前资产增量构建、先同步远端基座后增量构建和完全本机重建。后者按需加载 `requirements-semantic.txt` 中的 Torch 与 Sentence Transformers，基础或 ONNX-only 部署无需安装

AstrBot 导入卡组位于 `decks/astrbot_imports/<会话哈希>`。Link 不保存原始群号、用户号或会话 ID，查询接口只扫描调用方当前作用域；WebUI 和其他 AstrBot 会话只能看到 Link 本地卡组。卡组记录包含 `deck_ref`、显示名、主卡组、额外卡组、副卡组的卡名与数量，以及“当前 AstrBot 会话工具箱缓存”的来源标签

当前卡组修改接口还会校验对局尚未启动、作用域与已配置会话一致、目标确实是当前选中的 AstrBot 临时卡组。AstrBot 选择 Link 本地卡组时会先生成当前会话的临时副本，因此可以继续使用相同编辑接口；原始本地卡组和其他会话卡组不会被修改

Flutter 适合未来需要安装包、系统托盘、Android/iOS 原生能力或设备本地推理时再作为独立客户端。它不应成为 Link 服务端或外部 API 的前置依赖

仅远程控制 Link 的移动场景优先使用响应式 WebUI 或 PWA。仅 ONNX 的设备端推理场景需要另外完成 V3 编码器的 Dart 或原生实现，并通过 Android/iOS 原生 ONNX Runtime 接口接入；当前 Python ONNX 路径已经提供相同的 NumPy 输入契约和制品校验，可作为移植基准

参考资料

- [Docker Desktop 从容器访问宿主机服务](https://docs.docker.com/desktop/features/networking/networking-how-tos/)
- [Flutter 支持的平台](https://docs.flutter.dev/reference/supported-platforms)
- [ONNX Runtime Mobile](https://onnxruntime.ai/docs/get-started/with-mobile.html)
