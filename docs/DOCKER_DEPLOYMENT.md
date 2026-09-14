# 部署 Galatea Link

## Docker 在线一键部署

请先安装 Git、Docker Engine 或 Docker Desktop

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/Noctfom/Galatea-Link/main/scripts/install_docker.ps1 | iex
```

Linux 或 macOS：

```bash
curl -fsSL https://raw.githubusercontent.com/Noctfom/Galatea-Link/main/scripts/install_docker.sh | sh
```

打开 `http://127.0.0.1:8765`

查看运行状态：

```bash
docker compose ps
docker compose logs -f galatea-link
```

在线脚本会下载仓库、生成随机 Link API Token 并启动容器。WebUI 或 AstrBot 提示填写令牌时，从 `Galatea-Link/.env` 的 `GALATEA_LINK_API_TOKEN` 读取

## 本地一键自检启动

请先安装 Git 和 Python 3.11

```bash
git clone https://github.com/Noctfom/Galatea-Link.git
cd Galatea-Link
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File start_windows.ps1
```

Linux：

```bash
sh start_linux.sh
```

打开 `http://127.0.0.1:8765`

启动脚本会检查 Python、创建 `.venv`、按需安装 ONNX Runtime 依赖、生成本机配置并在校验通过后启动 Link

## Docker 服务结构

Docker 默认使用 Python 3.11 和 ONNX Runtime。WebUI、REST API、WebSocket 事件流及 AstrBot 桥接共用容器内的 TCP `8765` 端口，YGOPro 连接由 Link 主动向服务器发起，因此不需要额外映射游戏端口

首次启动会把公开的 `config.docker.yaml` 复制为数据卷中的 `/data/config.yaml`，以后启动不会覆盖用户配置。服务启动本身不会加载模型；先在 WebUI 导入卡组和 GKG，或分别上传 ONNX 模型及 V3 运行资产，再启动对局

## 端口与连接关系

默认只有一个入站端口：

| 调用方位置 | Link 地址 |
| --- | --- |
| 宿主机浏览器或宿主机 AstrBot | `http://127.0.0.1:8765` |
| 同一个 `galatea-link` Docker 网络中的 AstrBot | `http://galatea-link:8765` |
| 另一套 Docker Desktop Compose 中的 AstrBot | `http://host.docker.internal:8765` |
| 局域网其他设备 | `http://<Link宿主机地址>:8765` |

两端必须配置相同的 `GALATEA_LINK_API_TOKEN`。同一 Docker 网络内应使用服务名 `galatea-link`，不要固定容器 IP

如果 AstrBot 使用独立 Compose，可以把它接入 Link 创建的外部网络：

```yaml
services:
  astrbot:
    networks:
      - galatea-link

networks:
  galatea-link:
    external: true
    name: galatea-link
```

然后在 AstrBot 插件 Pages 中把 Link 地址设置为 `http://galatea-link:8765`

`GALATEA_LINK_PORT` 只修改宿主机发布端口。例如设置为 `18765` 后，宿主机和局域网客户端使用 `http://<宿主机>:18765`，同一 Docker 网络内的容器仍使用 `http://galatea-link:8765`

## 连接 YGOPro 服务器

Docker 默认配置中的 YGOPro 地址为 `host.docker.internal:7911`，用于连接运行在宿主机上的测试服务。Linux Compose 也通过 `host-gateway` 映射这个名称

- YGOPro 服务在宿主机：保留 `host.docker.internal` 并填写实际端口
- YGOPro 服务是在线服务器：直接填写域名或 IP
- YGOPro 服务在同一个 Docker 网络：填写其 Compose 服务名和容器端口

这是 Link 主动建立的出站 TCP 连接，不要为 YGOPro 在 Link 容器上额外开放端口

## 持久数据

Compose 使用命名卷 `galatea-link-data` 挂载到 `/data`。其中包含：

| 路径 | 内容 |
| --- | --- |
| `/data/config.yaml` | 容器基础配置 |
| `/data/link_state.json` | WebUI 非敏感设置与选择状态 |
| `/data/link_secrets.json` | WebUI 本机密钥存储 |
| `/data/decks/` | Link 本地卡组和 AstrBot 会话隔离卡组 |
| `/data/models/` | PTH 或 ONNX 模型 |
| `/data/model_assets/` | V3 CDB、Lua 与语义运行资产 |
| `/data/deploy_packages/` | 待导入或已保留的 GKG 包 |

`docker compose down` 不会删除命名卷，再次启动或升级镜像后数据仍然存在。不要使用 `docker compose down -v`，除非已经确认要永久删除全部 Link 数据

备份可以在停止服务后直接复制数据目录：

```bash
docker compose stop galatea-link
docker compose cp galatea-link:/data ./galatea-link-backup
docker compose start galatea-link
```

备份目录包含卡组、服务器配置和可能存在的 LLM Key，应按隐私数据保存，不要提交到 Git

## ONNX 与 PyTorch 镜像

Compose 默认安装 `requirements-onnx.txt`，适合 CPU ONNX 部署且镜像相对较小。它不会包含本机训练环境和语义重建所需的大型依赖

需要加载 PTH 模型时可以构建 PyTorch 变体：

```bash
docker build --build-arg LINK_REQUIREMENTS=requirements-pytorch.txt --build-arg LINK_VERSION=3.1.0 -t galatea-link:3.1.0-pytorch .
```

随后在自定义 Compose 中使用该镜像，并把 `config.yaml` 的 `model.inference_backend` 改为 `pytorch`。GPU 容器还需要宿主机 NVIDIA 驱动、Container Toolkit 和与设备匹配的 PyTorch 构建，本项目的默认 Compose 不预设 GPU 权限

完整的 Lua 语义重建依赖不放进默认运行镜像。生产部署更适合在开发机生成或同步资产，再通过 GKG 或 WebUI 导入容器

## 更新与回滚

更新源码后重新构建：

```bash
docker compose build --pull
docker compose up -d
```

命名卷不会因容器重建而消失。正式升级前仍建议备份 `/data`，并保留上一版镜像标签，以便配置或模型协议出现问题时回滚

## 本地手动部署细节

不使用一键脚本时，可以手动创建 Python 3.11 隔离环境。默认推荐 ONNX Runtime：

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-onnx.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe scripts/run_link_service.py --config config.yaml
```

Linux：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-onnx.txt
cp config.example.yaml config.yaml
.venv/bin/python scripts/run_link_service.py --config config.yaml
```

需要直接加载 PTH 模型时，把依赖文件改为 `requirements-pytorch.txt`。需要完整开发和语义构建环境时使用 `requirements.txt`

一键脚本也支持切换依赖配置：

```powershell
powershell -ExecutionPolicy Bypass -File start_windows.ps1 -Requirements requirements-pytorch.txt
```

```bash
GALATEA_LINK_REQUIREMENTS=requirements-pytorch.txt sh start_linux.sh
```

只执行环境检查而不启动服务：

```powershell
powershell -ExecutionPolicy Bypass -File start_windows.ps1 -CheckOnly
```

```bash
sh start_linux.sh --check-only
```

配置字段、模型目录和密钥优先级见 [配置参考](CONFIGURATION.md)

## 安全建议

- 不在 Dockerfile、镜像标签、Compose 文件或公开 YAML 中写入真实 API Key、房间密码或访问令牌
- 在线单命令会执行仓库 `main` 分支的安装脚本，需要先审查内容时请手动克隆仓库再运行 `scripts/deploy_docker.*`
- 局域网或跨机器访问必须使用强 Token，并通过防火墙限制 `8765` 的来源
- 不建议把 `8765` 直接暴露到公网；远程访问优先使用 VPN，或在支持 WebSocket 的 HTTPS 反向代理后提供服务
- 健康检查接口无需鉴权，但只返回版本和基本存活状态，不返回配置、卡组或对局观察
- 容器默认以非 root 用户运行、根文件系统只读，并移除 Linux capabilities

## 常见问题

### Compose 提示必须设置 Token

确认项目目录存在 `.env`，且 `GALATEA_LINK_API_TOKEN` 不是空字符串。修改后重新创建容器

### 容器健康检查失败

运行 `docker compose logs galatea-link`。常见原因是数据卷里的旧 `config.yaml` 端口被修改、访问令牌缺失，或配置 YAML 无法解析

### AstrBot 无法连接

先在 AstrBot 所在环境请求 `/api/v1/health`，再检查地址拓扑和两端 Token。容器内的 `127.0.0.1` 只代表该容器自身

### Link 无法连接宿主机 YGOPro

配置中使用 `host.docker.internal` 而不是 `127.0.0.1`，并确认 YGOPro 服务监听了 Docker 可访问的接口且防火墙允许连接

### 想直接查看宿主机数据目录

可以把 Compose 的命名卷改为绑定挂载，例如 `./runtime-data:/data`。这样更易手工备份，但 Linux 上需要保证目录可由容器 UID `10001` 写入，同时应把该目录排除版本控制

Docker 网络与持久卷行为可参考 [Compose 网络文档](https://docs.docker.com/compose/how-tos/networking/) 和 [Compose 卷文档](https://docs.docker.com/reference/compose-file/volumes/)
