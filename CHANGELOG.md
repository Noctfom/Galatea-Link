# Galatea Link 更新记录

本项目从 `3.0.0` 开始正式记录 Link 软件版本。此前开发阶段没有稳定版本号，下面按功能形成顺序保留简要里程碑

## 3.1.0 · 2026-09-13

- 增加默认 ONNX Runtime 的 Docker 镜像与 Docker Compose 部署方案
- 统一通过 `8765` 提供 WebUI、REST API、WebSocket 与 AstrBot 桥接
- 增加 `/data` 持久数据目录、非 root 运行、只读根文件系统和健康检查
- 增加 Windows 与 Linux 本地一键自检启动脚本，自动创建环境并安装推理依赖
- 增加 Windows、Linux 与 macOS 在线 Docker 单命令安装入口和独立部署文档
- 修复中文 Windows 旧版 pip 读取 UTF-8 依赖文件以及 MSYS Python 误识别问题

## 3.0.1 · 2026-09-13

- 隔离 Core、复杂动作预处理与 RuleBot 执行通道，增加本局 Core 超时熔断
- 修复 AstrBot 接管时强制切换 `llm_only`，现在只替换 LLM 后端并保留原决策模式
- 修复 YGOPro 卡组提交遗漏额外卡组以及卡组不合规原因不可见
- 修复 AstrBot 本地卡组没有转为当前会话临时副本的问题
- 修复临时卡组编辑字段兼容、修改后未重新上传以及部分会话配置丢失

## 3.0.0 · 2026-09-12

- 确立模型协议 V3 为当前稳定模型接入协议
- 支持 PyTorch、ONNX Runtime 与 GKG 部署包导入
- 完成 YGOPro 在线连接、版本协商、房间流程和对局结果识别
- 完成 Core、Hybrid、LLM Review 与 LLM Only 决策模式
- 完成 Link 本地 LLM 与 AstrBot 主智能体远程决策接入
- 增加浅色和深色 WebUI、统一配置、策略、模型与资产管理
- 增加 Link 本地卡组导入、内容查看、单卡调整和安全删除
- 增加游戏聊天、玩家可见观察、异步事件流和轻量赛后摘要
- 增加 CDB、Lua、语义资产与 MSG 142 兜底池管理
- 增加配置模板、隐私审计和公开发布文档

## 3.0.0 以前 · 未编号开发阶段

1. 打通 Galatea Core 与 YGOPro 游戏流程，完成基础消息解析和动作响应
2. 接入 OpenAI Compatible LLM，加入结构化动作、超时回退和缓存诊断
3. 建立异步运行时、宏观介入策略、动态覆盖和游戏聊天接口
4. 迁移模型协议 V3，加入 PTH、ONNX 与 GKG 独立适配
5. 建立 Link HTTP、WebSocket 与 WebUI 控制面
6. 完成 AstrBot 插件桥接、远程主智能体决策、会话卡组隔离和工具箱复用

后续版本从本文件顶部按倒序追加，并分别记录 Link 软件版本、外部 API 版本和模型协议版本，三者不混用
