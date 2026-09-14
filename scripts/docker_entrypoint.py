# Docker 启动入口，初始化持久目录并启动 Link 服务

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# 初始化数据卷目录并在首次启动时写入公开配置模板
def prepare_runtime_data(data_root: str | Path) -> Path:
    root = Path(data_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative_path in (
        "decks",
        "models",
        "model_assets/v3",
        "deploy_packages",
    ):
        (root / relative_path).mkdir(parents=True, exist_ok=True)

    config_path = root / "config.yaml"
    if config_path.exists() and not config_path.is_file():
        raise RuntimeError("Docker 数据卷中的 config.yaml 不是普通文件")
    if not config_path.exists():
        shutil.copyfile(PROJECT_ROOT / "config.docker.yaml", config_path)
    return config_path


# 使用当前 Python 进程替换为 Link 服务以正确接收容器停止信号
def main() -> None:
    data_root = os.getenv("GALATEA_LINK_DATA_DIR", "/data")
    config_path = prepare_runtime_data(data_root)
    os.environ["GALATEA_LINK_DATA_DIR"] = str(config_path.parent)
    os.execv(
        sys.executable,
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_link_service.py"),
            "--config",
            str(config_path),
        ],
    )


if __name__ == "__main__":
    main()
