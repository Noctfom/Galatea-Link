#!/usr/bin/env sh
# Galatea Link Linux 与 macOS 在线 Docker 安装脚本，下载仓库并调用快速部署入口

set -eu

if ! command -v git >/dev/null 2>&1; then
    echo "未找到 Git，请先安装 Git" >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "未找到 Docker，请先安装并启动 Docker Engine 或 Docker Desktop" >&2
    exit 1
fi

docker_install_current=$(pwd)
if [ -f "$docker_install_current/docker-compose.yml" ] && [ -f "$docker_install_current/scripts/deploy_docker.sh" ]; then
    docker_install_project=$docker_install_current
elif [ -n "${GALATEA_LINK_INSTALL_DIR:-}" ]; then
    docker_install_project=$GALATEA_LINK_INSTALL_DIR
else
    docker_install_project="$docker_install_current/Galatea-Link"
fi

if [ ! -e "$docker_install_project" ]; then
    git clone --depth 1 https://github.com/Noctfom/Galatea-Link.git "$docker_install_project"
elif [ ! -f "$docker_install_project/scripts/deploy_docker.sh" ]; then
    echo "目标目录已经存在但不是 Galatea Link 仓库：$docker_install_project" >&2
    exit 1
fi

exec sh "$docker_install_project/scripts/deploy_docker.sh"
