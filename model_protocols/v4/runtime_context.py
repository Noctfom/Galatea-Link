# V4 模型资产上下文，隔离词表语义目录和卡库且不修改外部 Core 的全局状态

from contextlib import contextmanager
from contextvars import ContextVar


_ASSETS = ContextVar("galatea_v4_assets", default=None)


# 读取当前调用已明确绑定的模型资产
def current_assets():
    assets = _ASSETS.get()
    if assets is None:
        raise RuntimeError("V4 推理必须绑定模型资产上下文")
    return assets


# 在当前任务或线程内临时绑定一套模型资产
@contextmanager
def use_assets(assets):
    token = _ASSETS.set(assets)
    try:
        yield assets
    finally:
        _ASSETS.reset(token)
