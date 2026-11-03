# V4 运行资产管理，验证精确词表并按推理后端处理语义内容更新

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json

from . import REQUIRED_ASSET_FILENAMES
from .card_vocab import load_card_vocabulary
from .runtime_context import use_assets
from .semantic_lookup import load_static_semantic_assets


PROTOCOL_IDENTITY_KEYS = (
    "model_protocol_version", "protocol_schema_revision", "protocol_schema_hash",
    "card_vocab_hash", "card_vocab_size", "card_vocab_card_count",
    "semantic_lookup_format_version", "semantic_lookup_hash", "semantic_lookup_card_count",
)


# 有界读取普通 JSON 文件并拒绝重复键
def read_json(path, max_bytes=2 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
        raise ValueError(f"V4 JSON 文件缺失或大小无效: {path.name}")
    # 保留唯一对象字段以防止校验与使用产生歧义
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"V4 JSON 包含重复字段: {key}")
            result[key] = value
        return result
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    if not isinstance(payload, dict):
        raise ValueError("V4 JSON 顶层必须是对象")
    return payload


@dataclass
class V4Assets:
    root: Path
    vocabulary: object
    lookup: object
    identity: dict
    _reader: object = None

    # 校验结构和词表前缀，按运行时策略处理语义内容差异
    def validate(self, metadata, label="V4 制品", *, allow_semantic_updates=False, frozen_onnx=False):
        from .protocol_schema import validate_protocol_metadata
        inspect_protocol_identity(metadata)
        with use_assets(self):
            if frozen_onnx:
                # 冻结图使用导出时语义，仅放宽外部内容哈希，保留结构与卡片编号前缀校验
                candidate = {**metadata, "semantic_lookup_hash": self.lookup.prefix_hash(metadata["card_vocab_card_count"])}
                return validate_protocol_metadata(candidate, label=label, card_vocabulary=self.vocabulary)
            if allow_semantic_updates and metadata.get("semantic_lookup_hash") != self.lookup.prefix_hash(metadata["card_vocab_card_count"]):
                # 先按实际语义哈希校验其他所有协议字段，再验证旧语义坐标系证据
                candidate = {**metadata, "semantic_lookup_hash": self.lookup.prefix_hash(metadata["card_vocab_card_count"])}
                validate_protocol_metadata(candidate, label=label, card_vocabulary=self.vocabulary)
                from .semantic_compatibility import validate_semantic_update_compatibility
                validate_semantic_update_compatibility(metadata, self.lookup, directory=self.root)
                return dict(self.identity)
            return validate_protocol_metadata(metadata, label=label, card_vocabulary=self.vocabulary)

    # 报告旧 ONNX 与新资产的内容差异，不宣称冻结图获得了新知识
    def onnx_compatibility(self, metadata):
        self.validate(metadata, frozen_onnx=True)
        added = self.vocabulary.card_count - metadata["card_vocab_card_count"]
        changed = metadata["semantic_lookup_hash"] != self.lookup.prefix_hash(metadata["card_vocab_card_count"])
        stale = bool(added or changed)
        return {"status": "frozen_onnx_outdated" if stale else "matched", "warning": stale,
            "added_card_count": added, "semantic_content_changed": changed,
            "frozen_card_count": metadata["card_vocab_card_count"],
            "unknown_new_cards": True,
            "message": "外部词表或语义已更新，继续使用旧 ONNX 图内语义；新增卡片按未知卡编码，既有卡片的语义修订也未写入图中" if stale else "ONNX 与运行资产一致"}

    # 延迟打开只读卡库且不影响纯元数据识别路径
    @property
    def card_reader(self):
        if self._reader is None:
            from utils.card_reader import CardReader
            candidate = self.root / "cards.cdb"
            self._reader = CardReader(candidate if candidate.is_file() else None, read_only=True)
        return self._reader

    # 释放由该模型资产独立持有的卡库句柄
    def close(self):
        if self._reader is not None:
            self._reader.close()
            self._reader = None


# 从指定目录读取并强制验证不可绕过的 V4 运行身份
def load_v4_assets(directory):
    root = Path(directory).expanduser().resolve()
    for name in REQUIRED_ASSET_FILENAMES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"V4 缺少运行资产: {name}")
    vocabulary = load_card_vocabulary(root / "card_vocab.json")
    lookup = load_static_semantic_assets(root, card_vocabulary=vocabulary, verify_logical_hash=True)
    assets = V4Assets(root, vocabulary, lookup, {})
    from .semantic_compatibility import read_semantic_compatibility_registry, merge_semantic_compatibility_registries
    registry = read_semantic_compatibility_registry(root)
    if registry is not None:
        merge_semantic_compatibility_registries(registry, {"format_version": 1, "bases": {},
            "category_to_index": lookup.category_to_index,
            "requirement_to_index": lookup.requirement_to_index})
    from .protocol_schema import get_current_protocol_metadata
    with use_assets(assets):
        assets.identity = get_current_protocol_metadata(vocabulary)
    return assets


# 限制协议元数据的字段类型并在分配网络前拒绝旧开发结构
def inspect_protocol_identity(metadata):
    from .protocol_schema import MODEL_PROTOCOL_VERSION, PROTOCOL_SCHEMA_REVISION
    if not isinstance(metadata, dict):
        raise ValueError("V4 协议元数据必须是对象")
    missing = set(PROTOCOL_IDENTITY_KEYS) - set(metadata)
    if missing:
        raise ValueError(f"V4 缺少协议身份字段: {sorted(missing)}")
    for key in ("model_protocol_version", "protocol_schema_revision", "card_vocab_size", "card_vocab_card_count", "semantic_lookup_format_version", "semantic_lookup_card_count"):
        if type(metadata[key]) is not int or metadata[key] < 0:
            raise ValueError(f"V4 协议字段类型无效: {key}")
    if metadata["model_protocol_version"] != MODEL_PROTOCOL_VERSION or metadata["protocol_schema_revision"] != PROTOCOL_SCHEMA_REVISION:
        raise ValueError("V4 观测结构修订不受支持")
    if metadata["semantic_lookup_format_version"] != 1 or metadata["card_vocab_card_count"] != metadata["semantic_lookup_card_count"]:
        raise ValueError("V4 语义与词表协议不一致")
    if not 10 < metadata["card_vocab_size"] <= 1_000_000 or metadata["card_vocab_card_count"] + 10 > metadata["card_vocab_size"]:
        raise ValueError("V4 词表容量无效")
    for key in ("protocol_schema_hash", "card_vocab_hash", "semantic_lookup_hash"):
        value = metadata[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"V4 摘要无效: {key}")
    # 结构身份只依赖固定字段与词表容量，不依赖词表内容或语义大表
    from types import SimpleNamespace
    from .protocol_schema import _schema_descriptor
    descriptor = _schema_descriptor(SimpleNamespace(capacity=metadata["card_vocab_size"]))
    expected = hashlib.sha256(json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if metadata["protocol_schema_hash"] != expected:
        raise ValueError("V4 protocol_schema_hash 与固定字段合同不一致")
    return {key: metadata[key] for key in PROTOCOL_IDENTITY_KEYS}
