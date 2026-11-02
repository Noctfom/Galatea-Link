# V4 编码器卡库代理，优先读取模型资产卡库并保持线程安全

from .runtime_context import current_assets


class ContextCardReader:
    # 将卡片查询转发给当前模型绑定的只读卡库
    def __getattr__(self, name):
        return getattr(current_assets().card_reader, name)


card_db = ContextCardReader()
