# Core 3.4.2 适配说明（历史归档）

本页保留旧版实现细节。当前 Link 已同时支持 Core 3.4.2 模型协议 V1 与稳定模型协议 V3，统一入口见 [MODEL_PROTOCOL_ADAPTERS.md](MODEL_PROTOCOL_ADAPTERS.md)

本文记录早期 Galatea Core `3.4.2` 的迁移背景，仅供追溯，不代表当前推荐配置。Link 与 Core 始终是独立项目，Link 运行时不会导入或读取 Core 源码，也不会修改 Core 目录

## 已迁移内容

- 66 维卡片物理特征和八位效果发动记忆
- 代码语义向量、紧凑条件索引和新版语义知识库
- FiLM、SwiGLU、自定义 Transformer、位置编码和新版策略价值头
- Core 3.4.2 检查点协议、UUID 校验和严格参数恢复
- `galatea_iter_30.pth` 模型及其身份清单
- 复杂选卡、祭品、求和、指示物、格子和排序的宏动作候选池
- P0/P1 资源视角转换、隐藏除外区与额外卡组身份隔离
- 活动区域同步时保留效果使用、装备和取对象等事件状态

Link 自己已有的装备关系、取对象关系、公开超量素材、选项描述编号、LLM 观察和异步决策接口均保留

## 有意保留的协议差异

Core 本地缓冲区可以带幽灵定界字节，MDPro/YGOPro 在线网络消息流不使用这套布局。Link 在 `core/gamestate.py` 中将 `CORE_HAS_GHOST_BYTE` 固定为 `False`，不会为 `MSG_CONFIRM_CARDS` 或多个连锁选项额外吞字节

这不是对 Core 3.4.2 的回退，而是本地核心缓冲区和在线协议输入源之间的适配边界

## 模型加载行为

默认配置使用以下身份

- 文件 `models/galatea_iter_30.pth`
- 检查点协议版本 `1`
- 模型 UUID `a204dd97-0f6f-46dc-9fb2-aba65df10e0c`
- 网络配置 `d_model=512`、`n_heads=8`、`n_layers=6`、`vocab_size=20000`

旧的 `galatea_iter_110.pth` 属于旧架构，没有检查点协议版本和模型 UUID，不能加载到 3.4.2 网络。Link 现在会明确拒绝它并关闭 Core 推理，不会继续使用随机初始化网络；LLM 和 RuleBot 仍可安全回退

替换新版模型时应同时更新 `model.weights_path` 和 `model.expected_model_id`。运行时可通过 `link.runtime.get_status()["core_model"]` 检查实际加载身份

## 本地验证

运行一次不连接服务器的严格加载和前向推理

```powershell
python scripts/test_core_model.py
```

运行全部单元测试

```powershell
python -m unittest discover -v
```

## 当时仍需真实 YGOPro 在线验证

- `prefer_second: false` 且猜拳获胜时应由 Bot 自己先攻
- `prefer_second: true` 且猜拳获胜时应由 Bot 自己后攻
- 多个连锁选项和确认卡片消息之后不应发生消息错位
- 至少选择两张卡、祭品、求和、移除指示物和排序操作应一次提交完整响应
- 新模型分别以 P0 和 P1 身份完成一局并保持隐藏信息隔离
- 游戏聊天收发和 AstrBot 反向事件消费需要真实服务器联调

在线验证前其余本地适配已经完成，但 Core 置信度仍是动作 softmax 分数而不是经过对局数据校准的胜率
