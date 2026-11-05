# DeepSeek Harness 接入

Link 可以通过独立的 `Galatea_Link_DSH` 插件接入 deepseek-harness。dsh 使用自己配置的模型、人格和当前会话，Link 负责 YGOPro 协议、Core 推理、合法动作和时限回退

近期 V4 和在线解析更新保持 API v1，已具备桥接能力的插件不要求与 Link 同步升级。工作区插件 0.1.1 的后续维护项和可选新功能见 [升级兼容方案](INTEGRATION_COMPATIBILITY.md)

## 交给 dsh 安装

向 dsh 提供插件目录，并发送：

> 请读取 Galatea_Link_DSH/skills/galatea-link-setup/SKILL.md，检查 Link 服务与插件依赖，安装到当前 profile，配置连接并验证状态。保留已有 Link 决策模式，列出当前会话可见卡组，再按我指定的服务器与房间开局

插件独立目录内提供 `README.md`、`AGENTS.md`、原生 `cordis.patch.yml` 和安装检查技能。安装时不改动 Galatea Core 或 dsh 内核

手动安装时，在插件目录执行 `npm install`，然后在 dsh 源码目录执行：

```powershell
pnpm dsh plugin --profile web add E:/Galatea_Link_DSH
```

重启当前 profile。使用 npm 安装版 dsh 时，命令前缀换成 `dsh plugin`

## Link 设置

在 WebUI 的“智能体来源”选择 DeepSeek Harness，或通过临时会话配置设置：

```yaml
decision:
  agent_backend: remote_dsh
  mode: hybrid
  llm_time_budget: 30.0
```

接入 dsh 仅替换 LLM 后端，不强制切换模式。`core_only` 不请求 dsh，`hybrid` 根据 Core 置信度和时点策略介入，`llm_review` 逐时点复核，`llm_only` 独立决策

启动 dsh 的进程需要读取 `GALATEA_LINK_API_TOKEN`，其值与 Link 相同。地址默认 `http://127.0.0.1:8765`，可通过 `GALATEA_LINK_URL` 或插件 `baseUrl` 修改。只有 Link URL 和 Token 跨进程传输配置，Link 不读取 dsh 模型 Key，也不需要额外配置一套 LLM

连接成功后，Link WebUI 会分别显示 AstrBot 与 dsh 心跳；插件 `galatea_link_status` 显示事件流连接状态。不同主机或 Docker 部署仍共用 `8765` 端口，网络方案见 [部署文档](DOCKER_DEPLOYMENT.md)

## 对局能力

插件提供状态、卡组列表及内容、YDK 导入、服务器和卡组选择、启动停止、玩家可见观察、游戏聊天、临时卡组编辑、策略调整与动作提交工具。配置模式时沿用原值，选择本地卡组时先复制为当前 dsh 会话的临时副本

后台游戏任务通过 dsh 自身执行器继承当前模型、人格和已完成的会话上下文，原聊天会话可以继续与用户交流。动作返回前再次核对远程会话、请求与观察身份，过期任务立即取消；游戏聊天按序号去重，并在决策到来时让出执行资源

对局结果形成一次轻量摘要，写入发起 dsh 会话的待消费上下文。摘要包含胜负或断线原因、动作和聊天数量，不逐动作复制完整牌局

## 新增接口

- `decision.agent_backend` 新增 `remote_dsh`
- `POST /api/v1/integrations/dsh/heartbeat` 登记实例、开关与 60 秒租约
- 状态接口新增 `integrations.dsh`
- 能力接口返回 `remote_agent_decisions.backends`
- `agent.decision.requested` 与 pending 查询新增 `agent_backend`
- dsh 提交动作必须包含 `agent_backend: remote_dsh`，避免旧 AstrBot 客户端误接管

HTTP、事件流、临时会话、卡组、动态控制和决策身份接口继续使用 API v1。临时卡组沿用 v1 的内部目录和 `astrbot:` 引用前缀，dsh 使用独立的散列作用域，并在来源元数据中明确标记 dsh，不会查询其他 dsh 或 AstrBot 会话卡组

## 版本与验证边界

桥接能力从 Link `3.2.0` 提供，Link 当前版本见根目录更新记录。工作区插件版本独立为 `0.1.1`；下述运行验证使用了最初的 `0.1.0` 桥接实现，不代表已经重新验收后续插件改动。既有适配覆盖本地 dsh `0.1.7-rc.1` 的 `subagents.start`，以及 `0.2.1-rc.2` 的 `subagents.startActivation`

本地旧版本已经使用真实 Cordis、工具注册器、AgentLoop 和 fork 后端测试，通过确定性模型确认上下文继承、结构化动作和摘要注入；HTTP/WebSocket 闭环也由真实 Link 服务验证。现代入口通过接口契约测试，尚未在升级后的完整 dsh 实例运行。真实模型延迟、实际游戏服务器与长时间对局需要现场联调

显式开启 `allowAgentDeckEdit` 后，dsh 才能修改当前临时对战卡组；原文件和其他会话副本保持隔离。当前未提供在线 BO3 换备或 AstrBot 工具箱的查裁定模块，dsh 可复用自己已有的搜索与文件工具准备 YDK

协议来源：[官方插件安装文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md)、[官方工具协议](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/adding-a-tool.md)
