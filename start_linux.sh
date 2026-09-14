#!/usr/bin/env sh
# Galatea Link Linux 本地一键自检启动脚本，准备 ONNX 环境并启动服务

set -eu

local_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$local_script_dir"

local_check_only=0
if [ "${1:-}" = "--check-only" ]; then
    local_check_only=1
elif [ "$#" -gt 0 ]; then
    echo "用法：sh start_linux.sh [--check-only]" >&2
    exit 2
fi

local_requirements=${GALATEA_LINK_REQUIREMENTS:-requirements-onnx.txt}
case "$local_requirements" in
    requirements-pytorch.txt)
        local_backend_module=torch
        ;;
    requirements-onnx.txt|requirements.txt)
        local_backend_module=onnxruntime
        ;;
    *)
        echo "GALATEA_LINK_REQUIREMENTS 只能选择公开依赖配置" >&2
        exit 2
        ;;
esac

local_bootstrap_python=""
for local_python_candidate in python3.11 python3 python; do
    if command -v "$local_python_candidate" >/dev/null 2>&1; then
        if "$local_python_candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            local_bootstrap_python=$local_python_candidate
            break
        fi
    fi
done
if [ -z "$local_bootstrap_python" ]; then
    echo "未找到 Python 3.11 或更高版本" >&2
    exit 1
fi

local_venv_root="$local_script_dir/.venv"
local_venv_python="$local_venv_root/bin/python"
if [ ! -x "$local_venv_python" ]; then
    echo "正在创建 Galatea Link Python 隔离环境"
    "$local_bootstrap_python" -m venv "$local_venv_root"
fi

local_requirement_fingerprint=$(
    "$local_venv_python" -c 'import hashlib, pathlib, sys; files=sorted(pathlib.Path(".").glob("requirements*.txt")); digest=hashlib.sha256(); [digest.update(path.name.encode()+b"\0"+path.read_bytes()+b"\0") for path in files]; print(sys.argv[1]+"|"+digest.hexdigest())' "$local_requirements"
)
local_requirement_stamp="$local_venv_root/.galatea-requirements.sha256"
local_installed_fingerprint=""
if [ -f "$local_requirement_stamp" ]; then
    local_installed_fingerprint=$(tr -d '\r\n' <"$local_requirement_stamp")
fi

if [ "$local_installed_fingerprint" != "$local_requirement_fingerprint" ]; then
    echo "正在安装或更新 $local_requirements"
    "$local_venv_python" -m pip install --disable-pip-version-check -r "$local_requirements"
    printf '%s' "$local_requirement_fingerprint" >"$local_requirement_stamp"
else
    echo "Python 依赖自检通过"
fi

if [ ! -f "$local_script_dir/config.yaml" ]; then
    cp "$local_script_dir/config.example.yaml" "$local_script_dir/config.yaml"
    echo "已生成本机 config.yaml"
fi

mkdir -p decks models model_assets/v3 deploy_packages

local_web_address=$("$local_venv_python" -c "import aiohttp, httpx, numpy, yaml, $local_backend_module; from app_config import load_app_config; config=load_app_config('config.yaml'); host='127.0.0.1' if config.service.host in ('0.0.0.0', '::') else config.service.host; print(f'http://{host}:{config.service.port}')")

if [ "$local_check_only" -eq 1 ]; then
    echo "Galatea Link 本地环境自检完成"
    exit 0
fi

echo "正在启动 Galatea Link：$local_web_address"
exec "$local_venv_python" scripts/run_link_service.py --config config.yaml
