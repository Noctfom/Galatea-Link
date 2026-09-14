#!/usr/bin/env sh
# Galatea Link Linux 与 macOS Docker 快速部署脚本，初始化访问令牌并启动服务

set -eu

deploy_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
deploy_project_root=$(dirname -- "$deploy_script_dir")
cd "$deploy_project_root"

if ! command -v docker >/dev/null 2>&1; then
    echo "未找到 Docker，请先安装并启动 Docker Engine 或 Docker Desktop" >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "当前 Docker 未提供 Compose v2" >&2
    exit 1
fi

deploy_env_path="$deploy_project_root/.env"
if [ ! -f "$deploy_env_path" ]; then
    cp "$deploy_project_root/.env.example" "$deploy_env_path"
fi

if ! grep -Eq '^GALATEA_LINK_API_TOKEN=[^[:space:]]+' "$deploy_env_path"; then
    if command -v openssl >/dev/null 2>&1; then
        deploy_token=$(openssl rand -hex 32)
    else
        deploy_token=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    fi
    deploy_temp_path=$(mktemp)
    trap 'rm -f "$deploy_temp_path"' EXIT HUP INT TERM
    awk -v token="$deploy_token" '
        BEGIN { replaced = 0 }
        /^GALATEA_LINK_API_TOKEN=/ {
            print "GALATEA_LINK_API_TOKEN=" token
            replaced = 1
            next
        }
        { print }
        END {
            if (!replaced) {
                print "GALATEA_LINK_API_TOKEN=" token
            }
        }
    ' "$deploy_env_path" >"$deploy_temp_path"
    mv "$deploy_temp_path" "$deploy_env_path"
    trap - EXIT HUP INT TERM
    chmod 600 "$deploy_env_path"
    echo "已在 .env 中生成随机 Link API Token"
fi

docker compose up -d --build

deploy_port=$(awk -F= '/^GALATEA_LINK_PORT=[0-9]+$/ { print $2 }' "$deploy_env_path" | tail -n 1)
if [ -z "$deploy_port" ]; then
    deploy_port=8765
fi

echo "Galatea Link 已启动：http://127.0.0.1:$deploy_port"
echo "查看日志：docker compose logs -f galatea-link"
