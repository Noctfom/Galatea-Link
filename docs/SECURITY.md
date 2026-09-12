# 安全与隐私

Galatea Link 会接触 LLM API Key、Link 控制令牌、游戏房间密码、用户卡组、游戏聊天和玩家可见对局状态。公开仓库、Issue 和日志中都不应包含这些运行数据

## 不应提交的内容

以下路径已由根目录 `.gitignore` 排除：

- `config.yaml` 与本机变体
- `.env` 和 `.env.*`
- `link_secrets.json`
- `link_state.json`
- `decks/` 及 AstrBot 会话卡组副本
- `data/` 中的 AstrBot 运行态配置、缓存和渲染模板
- `models/`、`model_assets/`、`deploy_packages/` 和常见模型制品扩展名
- 日志、崩溃转储、临时文件和 Python 缓存

公开配置只使用 `config.example.yaml` 和 `.env.example`，两者的敏感字段必须保持为空

`.gitignore` 不能保护已经被 Git 跟踪的文件。如果某个敏感文件曾经加入索引，应在保留本地文件的同时执行：

```bash
git rm --cached config.yaml
```

如果真实密钥已经进入提交历史，仅从最新提交删除并不安全。应立即吊销并轮换该密钥，再根据仓库协作情况使用历史清理工具处理旧提交

## 推荐的密钥方式

LLM API Key：

1. 首选 `GALATEA_LLM_API_KEY` 环境变量
2. 次选 Link WebUI 写入的本机 `link_secrets.json`
3. 不推荐把 Key 写入 `config.yaml`

Link 控制令牌：

1. 让 `service.api_token` 保持为空
2. 通过 `GALATEA_LINK_API_TOKEN` 或 `service.api_token_env` 指定的变量注入
3. AstrBot 与 Link 两端配置相同令牌

房间密码可以保存在本机 `config.yaml`，也可以由 AstrBot 在启动前通过临时会话接口提交。状态接口、事件和普通持久化文件不会返回房间密码明文

## 网络暴露

- 单机使用时让 `service.host` 保持 `127.0.0.1`
- Docker 或局域网接入时才改为 `0.0.0.0`
- 非本机监听必须设置访问令牌，Link 启动时也会执行这项校验
- 不要把纯 HTTP 控制端口直接映射到公网
- 跨设备使用时优先放在可信局域网、VPN 或带 TLS 的反向代理后
- `allowed_origins` 只限制浏览器 Origin，不能替代防火墙和鉴权

## 卡组与会话隔离

Link 本地卡组不会通过 AstrBot 工具暴露给任意其他会话。AstrBot 导入卡组使用不可逆会话哈希作为作用域，只能列出、选择和修改当前会话的临时副本

临时卡组带过期时间并由清理逻辑删除。不要把 `decks/astrbot_imports/`、工具箱 `deck_cache/` 或对应元数据作为问题附件上传

## 日志与诊断

Link 的 LLM 请求诊断不会主动打印 Authorization 或 API Key，但日志仍可能包含：

- 模型名和服务地址
- 卡片名称、合法动作与玩家可见盘面
- 游戏聊天正文
- 会话错误、服务器地址和房间生命周期

提交日志前应人工检查并删除聊天、服务器地址、用户标识、卡组来源和任何供应商返回的敏感错误内容。协议十六进制载荷也可能包含玩家可见数据，不应默认公开

## 上传前检查

```bash
git status --short --ignored
git diff --check
git ls-files config.yaml link_state.json link_secrets.json .env
python scripts/audit_release_privacy.py
```

`git ls-files` 正常情况下不应输出任何敏感运行文件。隐私审计脚本只报告文件名、提交短哈希和风险类型，不会输出检测到的密钥值；返回码为 1 表示当前文件或历史提交仍有风险需要人工处理。还应检查暂存区：

```bash
git diff --cached --name-only
git diff --cached --check
```

仓库当前会保留 `cards.cdb`、`knowledge_base.json` 和源码内的公开示例地址；它们不是运行密钥。模型权重、用户卡组和本机生成向量则默认不进入版本库
