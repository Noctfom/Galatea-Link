# GKG 部署包导入

在 WebUI 的模型仓库选择 `.gkg` 并导入，也可以把包放进 `deploy_packages/`，从页面的本地部署包列表导入。后者适合较大的模型，不需要重复上传

| 容器格式 | 模型协议 | Link 行为 |
| --- | --- | --- |
| 2 | V3 | 导入模型及独立运行资产 |
| 4 | V4 | 导入精确词表、编译语义及模型 |
| 5 | V4 | 在格式 4 的模型校验基础上核验并归档附件 |

GKG5 扩展的是容器，不是模型输入结构。V4 从 Link 3.3.9 起支持玩家可见状态兼容在线决策，无法对齐的转移历史默认屏蔽；导入模型不代表原生观测完整性或实际策略质量已验收，详见 [V4 接入说明](V4_ADAPTER.md)

## GKG5 附件

Link 3.3.7 按 Core 当前公开容器合同读取 `optional_content.format_version=1`，支持 `decks`、`replays`、`documents`、`presets` 四类。导入前逐项核对路径、分类、大小、SHA256 和实际文件集合

附件保存到 `deploy_packages/imported/<包名>-<清单摘要>/`，例如：

```text
contents.json
content/decks/测试池/构筑.ydk
content/replays/raw/duel.core.jsonl.gz
content/documents/README.md
content/documents/images/example.png
content/presets/deployment.yaml
```

相对目录、中文文件名、图片链接和原始字节均保留。`contents.json` 记录包名、模型引用、原始路径、大小和摘要，归档分组由清单稳定生成。相同包重复导入会复用原归档；已有文件或来源清单内容不同则拒绝覆盖

附件默认**核验与归档**，卡组提供额外的显式入库选择：

- 卡组不会自动成为当前对战卡组；WebUI 发现包内卡组后会询问是否加入本地仓库，取消则只保留附件归档
- 确认后创建 `decks/gkg_imports/<包身份摘要>/` 下的可编辑副本，列表显示包名和原始相对路径，可查看、选用、单卡修改和删除；包内原件保持不变，重复入库保留已有副本及本地修改
- 入库后需要在卡组仓库中显式选择用于对战的卡组；AstrBot／dsh 选用时可以复制为各自当前会话的临时卡组，不改变原有会话隔离
- 重放保留文件，不新增重放格式解析器
- 文档和图片保留本地文件，不通过服务接口渲染 HTML 或 SVG
- 预设只保存文件，不读取参数、不替换 LLM/API Key、不切换模式或温度；后续合同稳定后再讨论应用流程

WebUI 导入后显示附件数量和归档位置。`deploy_packages/` 已被 Git 忽略，附件不会随代码自动提交；分享归档或原包时仍需自行检查其内容

附件复制失败会清理本次附件暂存，不留下部分成功的归档。模型、运行资产与附件安装不构成整包事务，后续安装步骤失败时，先前成功安装的模型或资产可能已经存在，修正后可以重新导入

## 外部接口

现有上传和本地导入接口保持不变，结果的 `imported` 新增：

```json
{
  "package_format_version": 5,
  "model_protocol_version": 4,
  "content": {
    "archive_path": "./deploy_packages/imported/Shared-<清单摘要>",
    "archive_id": "Shared-<清单摘要>",
    "presets_applied": false,
    "files": [
      {
        "category": "decks",
        "path": "content/decks/Pool/A.ydk",
        "relative_path": "Pool/A.ydk",
        "size": 23,
        "sha256": "<64位小写十六进制摘要>"
      }
    ]
  }
}
```

旧包返回空 `files`、`archive_path=null` 和 `archive_id=null`。服务能力声明的 `gkg_package_format_versions` 为 `[2,4,5]`，默认附件处理仍为 `gkg_optional_content=archive_only`，另以 `gkg_deck_import=explicit_local_copy` 声明可选卡组入库。AstrBot／dsh 无需增加深度学习依赖或同步更改现有决策接口

明确选择后调用 `POST /api/v1/models/import-decks`，需要与其他私有接口相同的鉴权，JSON 参数为 `{"archive_id":"Shared-<清单摘要>"}`。标识只能使用模型导入结果中的 `archive_id`，不接受客户端任意文件路径

返回 `installed` 新增卡组引用、`preserved` 保留的已有引用、`skipped` 无法入库的卡组及原因，以及最新 `catalog`。接口不更改当前模型或卡组选择；没有再次上传 GKG 的必要。归档卡组被修改或摘要不符会拒绝入库，不合法或过大的 YDK 会单独跳过并反馈，不撤销已经完成的模型导入

`gkg:<包身份摘要>:<卡组路径摘要>` 是稳定本地卡组引用，可通过原有配置和本地卡组管理接口选用、编辑或删除。普通 YDK 导入仍保存在 `decks` 根目录，不覆盖 GKG 副本

## 文件限制

模型及根资产维持原单层文件名规则。只有附件允许进入 `content/<category>/`，拒绝越界路径、链接、文件目录冲突及大小写／Unicode 等价冲突

旧包最多 256 个成员、清单 2 MiB；GKG5 最多 16640 个成员，其中附件清单最多 16384 个文件、清单 32 MiB。包文件和展开总量均不超过 64 GiB，单成员不超过 32 GiB，保留既有压缩比限制。这些是上限，不表示建议通过浏览器上传最大规模的包

模型和运行资产的更多说明见 [模型协议适配](MODEL_PROTOCOL_ADAPTERS.md#gkg-导入和模型选择)
