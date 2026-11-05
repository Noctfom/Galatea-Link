# 在线协议兼容日志模块，将非致命差异保存到有限大小的本地日志

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGGER = logging.getLogger("galatea_link.protocol")


# 在程序入口配置轮转日志，文件不可写时只发出警告而不阻止服务启动
def configure_protocol_warning_log(data_root):
    target = Path(data_root) / "logs" / "protocol_warnings.log"
    if any(isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == target.resolve() for handler in LOGGER.handlers):
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(target, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.WARNING)
    except OSError as error:
        LOGGER.warning("无法保存在线协议兼容日志，将继续运行: %s", error)
