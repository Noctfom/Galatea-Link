# 模型协议 V3 旧入口，转发到不绑定 Core 发布版本的通用导入脚本

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.import_core_v3_bundle import main


if __name__ == "__main__":
    main()
