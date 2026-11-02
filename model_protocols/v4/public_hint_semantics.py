# V4 提示目录身份与只读来源查询，固定复用 Core V4 协议且不依赖外部 Core 目录

PUBLIC_HINT_BINDING_FORMAT_VERSION = 1

# 把提示目录序列化为安全且确定的整数四元组
def serialize_public_hint_catalog(catalog):
    """把提示目录序列化为安全且确定的整数四元组"""
    return [[desc, *record] for desc, record in sorted(catalog.items())]

# 验证提示来源、槽位与绑定级别，拒绝重复身份和越界记录
def deserialize_public_hint_catalog(records):
    """验证提示来源、槽位与绑定级别，拒绝重复身份和越界记录"""
    if not isinstance(records, list) or len(records) > 200000:
        raise ValueError("invalid public hint catalog")
    catalog = {}
    for record in records:
        if not isinstance(record, list) or len(record) != 4:
            raise ValueError("invalid public hint catalog record")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in record):
            raise ValueError("public hint catalog values must be integers")
        desc, card, slot, binding = record
        if not 0 < desc <= 0xFFFFFFFF or not 0 < card <= 0x7FFFFFFF or not 0 <= slot < 8 or binding not in (1, 2) or desc in catalog:
            raise ValueError("invalid or duplicate public hint binding")
        catalog[desc] = (card, slot, binding)
    return catalog

# 在当前模型资产中解析提示的精确来源
# 复用 Core V4 的 resolve_public_hint 协议逻辑
def resolve_public_hint(desc):
    from .runtime_context import current_assets
    return current_assets().lookup.public_hint_bindings.get(int(desc), (0, -1, 0))
