# GKG 部署包导入

在 WebUI 的模型仓库选择 `.gkg` 并导入，也可以把包放进 `deploy_packages/`，从页面的本地部署包列表导入。后者适合较大的模型，不需要重复上传

| 容器格式 | 模型协议 | Link 行为 |
| --- | --- | --- |
| 2 | V3 | 导入模型及独立运行资产 |
| 4 | V4 | 导入精确词表、编译语义及模型 |
| 5 | V4 | 在格式 4 的模型校验基础上核验并归档附件 |

GKG5 扩展的是容器，不是模型输入结构。导入 V4 不代表已开放在线模型决策；当前在线仍可使用 V3 或 `llm_only`，详见 [V4 接入说明](V4_ADAPTER.md)

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

这一阶段的附件处理是**核验与归档**：

- 卡组不会自动成为当前对战卡组，也暂不自动加入 WebUI 的本地卡组列表；需要使用时，可从归档取出 YDK，通过已有卡组导入界面导入
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

旧包返回空 `files` 和 `archive_path=null`。服务能力声明的 `gkg_package_format_versions` 为 `[2,4,5]`，`gkg_optional_content=archive_only`。AstrBot／dsh 无需增加深度学习依赖或同步更改现有决策接口

## 文件限制

模型及根资产维持原单层文件名规则。只有附件允许进入 `content/<category>/`，拒绝越界路径、链接、文件目录冲突及大小写／Unicode 等价冲突

旧包最多 256 个成员、清单 2 MiB；GKG5 最多 16640 个成员，其中附件清单最多 16384 个文件、清单 32 MiB。包文件和展开总量均不超过 64 GiB，单成员不超过 32 GiB，保留既有压缩比限制。这些是上限，不表示建议通过浏览器上传最大规模的包

模型和运行资产的更多说明见 [模型协议适配](MODEL_PROTOCOL_ADAPTERS.md#gkg-导入和模型选择)
