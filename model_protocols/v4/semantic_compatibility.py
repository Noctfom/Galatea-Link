# V4 语义兼容证据只读校验，参考 Core f83f700 且不包含编译或写入入口

import hashlib
import json
from pathlib import Path

SEMANTIC_COMPATIBILITY_FILENAME = "semantic_compatibility_v1.json"
SEMANTIC_CATEGORY_CAPACITY = 4000
SEMANTIC_REQUIREMENT_CAPACITY = 128
MAX_STATIC_SEMANTIC_CATALOG_BYTES = 16 * 1024 * 1024

# 只对标签编号与编码器合同求指纹，不把效果内容更新视为网络结构变化
def _semantic_json_hash(value):
    """只对标签编号与编码器合同求指纹，不把效果内容更新视为网络结构变化"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()

# 稳定语义编号必须连续、唯一且保持网络容量和保留槽不变
def _validate_semantic_labels(mapping, *, category):
    """稳定语义编号必须连续、唯一且保持网络容量和保留槽不变"""
    capacity = SEMANTIC_CATEGORY_CAPACITY if category else SEMANTIC_REQUIREMENT_CAPACITY
    if not isinstance(mapping, dict) or len(mapping) > capacity:
        raise ValueError('semantic label registry exceeds network capacity')
    if any(not isinstance(k, str) or not k or type(v) is not int for k, v in mapping.items()):
        raise ValueError('invalid semantic label registry')
    if sorted(mapping.values()) != list(range(len(mapping))):
        raise ValueError('semantic label registry must be an append-only contiguous mapping')
    if category and (mapping.get('<PAD>') != 0 or mapping.get('<UNK>') != 1):
        raise ValueError('semantic label registry reserved slots changed')

# 只读稳定编号和已核验历史基座，不猜测未知旧检查点的语义来源
def read_semantic_compatibility_registry(directory='.'):
    """只读稳定编号和已核验历史基座，不猜测未知旧检查点的语义来源"""
    path = Path(directory) / SEMANTIC_COMPATIBILITY_FILENAME
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > MAX_STATIC_SEMANTIC_CATALOG_BYTES:
        raise ValueError('invalid semantic compatibility registry file')
    from .assets import read_json
    data = read_json(path, max_bytes=MAX_STATIC_SEMANTIC_CATALOG_BYTES)
    if not isinstance(data, dict) or data.get('format_version') != 1 or not isinstance(data.get('bases'), dict):
        raise ValueError('invalid semantic compatibility registry format')
    _validate_semantic_labels(data.get('category_to_index'), category=True)
    _validate_semantic_labels(data.get('requirement_to_index'), category=False)
    for identity, descriptor in data['bases'].items():
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('invalid historical semantic identity')
        if not isinstance(descriptor, dict) or descriptor.get('semantic_lookup_hash') != identity:
            raise ValueError('historical semantic identity disagrees with its descriptor')
    return data

# 同步只允许同一编号前缀扩展，不合并来自独立分支的编号冲突
def merge_semantic_compatibility_registries(existing, incoming):
    """同步只允许同一编号前缀扩展，不合并来自独立分支的编号冲突"""
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    result = {'format_version': 1, 'bases': dict(existing['bases'])}
    for name in ('category_to_index', 'requirement_to_index'):
        left, right = existing[name], incoming[name]
        shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
        if any(longer.get(label) != index for label, index in shorter.items()):
            raise ValueError('semantic label registries have conflicting indices; cannot replace learned meanings')
        result[name] = dict(longer)
    for identity, descriptor in incoming['bases'].items():
        prior = result['bases'].get(identity)
        if prior is not None and any(prior.get(k) != descriptor.get(k) for k in
                ('encoder', 'policy', 'card_count', 'card_vocab_hash', 'code_dimension')):
            raise ValueError('conflicting semantic compatibility evidence')
        result['bases'].setdefault(identity, descriptor)
    return result

# 描述学习参数依赖的编号和向量坐标系，内容哈希继续单独留存
def semantic_compatibility_descriptor(lookup):
    """描述学习参数依赖的编号和向量坐标系，内容哈希继续单独留存"""
    if not lookup.code_encoder or not lookup.code_policy:
        return None
    return {'format_version': 1, 'semantic_lookup_hash': lookup.prefix_hash(),
        'card_vocab_hash': lookup.card_vocabulary.vocabulary_hash,
        'card_count': lookup.card_vocabulary.card_count,
        'category_count': len(lookup.category_to_index),
        'category_prefix_hash': _semantic_json_hash(lookup.category_to_index),
        'requirement_count': len(lookup.requirement_to_index),
        'requirement_prefix_hash': _semantic_json_hash(lookup.requirement_to_index),
        'encoder': lookup.code_encoder, 'policy': lookup.code_policy,
        'code_dimension': int(lookup.code_dictionary.shape[1])}

# 允许内容演进，但禁止卡密、效果标签或向量编码器坐标系被静默替换
def validate_semantic_update_compatibility(container, lookup, *, directory='.'):
    """允许内容演进，但禁止卡密、效果标签或向量编码器坐标系被静默替换"""
    identity = container.get('semantic_lookup_hash')
    descriptor = container.get('semantic_compatibility')
    if descriptor is None and isinstance(container.get('net_config'), dict):
        descriptor = container['net_config'].get('semantic_compatibility')
    if descriptor is None:
        registry = read_semantic_compatibility_registry(directory)
        descriptor = registry['bases'].get(identity) if registry else None
    if not isinstance(descriptor, dict) or descriptor.get('format_version') != 1:
        raise ValueError('semantic content changed without verified compatibility evidence; preserve the original assets')
    if (descriptor.get('semantic_lookup_hash') != identity
            or descriptor.get('card_vocab_hash') != container.get('card_vocab_hash')
            or descriptor.get('card_count') != container.get('semantic_lookup_card_count')):
        raise ValueError('semantic compatibility descriptor does not match the checkpoint identity')
    for group, mapping in (('category', lookup.category_to_index), ('requirement', lookup.requirement_to_index)):
        count = descriptor.get(group + '_count')
        if type(count) is not int or not 0 <= count <= len(mapping):
            raise ValueError(f'semantic {group} prefix is incompatible')
        prefix = {label: index for label, index in mapping.items() if index < count}
        if _semantic_json_hash(prefix) != descriptor.get(group + '_prefix_hash'):
            raise ValueError(f'semantic {group} labels have been renumbered')
    if (not lookup.code_encoder or not lookup.code_policy
            or descriptor.get('encoder') != lookup.code_encoder
            or descriptor.get('policy') != lookup.code_policy
            or descriptor.get('code_dimension') != lookup.code_dictionary.shape[1]):
        raise ValueError('semantic vector encoder or chunk policy changed; weights require an explicit migration')
    return descriptor
