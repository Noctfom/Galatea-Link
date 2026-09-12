# Link 独立服务启动脚本，允许从任意工作目录运行远程控制端

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from service.http_api import run_http_service
from link_version import __version__


# 解析服务配置文件命令行参数
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 Galatea Link 独立服务")
    parser.add_argument(
        "--version",
        action="version",
        version=f"Galatea Link {__version__}",
    )
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "config.yaml"),
        help="Link YAML 配置文件路径",
    )
    return parser.parse_args()


# 启动 HTTP、WebSocket 和 WebUI 服务
def main() -> None:
    args = parse_args()
    print(f"🌐 Galatea Link v{__version__}")
    run_http_service(args.config)


if __name__ == "__main__":
    main()
