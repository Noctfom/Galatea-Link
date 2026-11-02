# V4 编译语义只读加载与逻辑身份校验，固定复用 Core V4 协议且不依赖外部 Core 目录

import hashlib
import json
import zipfile
from pathlib import Path
import numpy as np
from .card_vocab import FIRST_CARD_TOKEN_ID, get_default_card_vocabulary
from .public_hint_semantics import PUBLIC_HINT_BINDING_FORMAT_VERSION, serialize_public_hint_catalog, deserialize_public_hint_catalog
STATIC_SEMANTIC_TABLE_FILENAME = 'semantic_lookup_v1.npz'
STATIC_SEMANTIC_CATALOG_FILENAME = 'semantic_lookup_v1.json'
CODE_EMBEDDINGS_FILENAME = 'code_embeddings.npy'
CODE_EMBEDDINGS_INDEX_FILENAME = 'code_embeddings_idx.json'

STATIC_SEMANTIC_LOOKUP_FORMAT_VERSION = 1

SEMANTIC_EFFECT_SLOTS = 8

SEMANTIC_CATEGORY_SLOTS = 8

SEMANTIC_REQUIREMENT_SLOTS = 16

SEMANTIC_RELATION_SLOTS = 4

SEMANTIC_REQUIREMENT_CAPACITY = 128

SEMANTIC_CATEGORY_CAPACITY = 4000

MAX_STATIC_SEMANTIC_TABLE_BYTES = 1024 * 1024 * 1024

MAX_STATIC_SEMANTIC_CATALOG_BYTES = 16 * 1024 * 1024

# 把数值数组转换为跨平台稳定的小端连续字节
def _canonical_array_bytes(array):
    """把数值数组转换为跨平台稳定的小端连续字节"""
    dtype = array.dtype
    if dtype.byteorder not in ("|", "<"):
        dtype = dtype.newbyteorder("<")
    return np.ascontiguousarray(array, dtype=dtype).tobytes(order="C")

# 流式计算编译资产摘要，避免校验大表时制造额外整文件副本
def _sha256_file(path):
    """流式计算编译资产摘要，避免校验大表时制造额外整文件副本"""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

# 返回静态语义数值表与轻量运行目录的固定路径
def _compiled_asset_paths(directory):
    """返回静态语义数值表与轻量运行目录的固定路径"""
    root = Path(directory).resolve()
    return (
        root / STATIC_SEMANTIC_TABLE_FILENAME,
        root / STATIC_SEMANTIC_CATALOG_FILENAME,
    )

# 严格还原运行时效果绑定，并拒绝重复键或越界槽位
def _deserialize_runtime_bindings(records):
    """严格还原运行时效果绑定，并拒绝重复键或越界槽位"""
    if not isinstance(records, list):
        raise ValueError("static semantic runtime bindings must be a list")
    bindings = {}
    for record in records:
        if not isinstance(record, list) or len(record) != 3:
            raise ValueError("static semantic runtime binding must be a triple")
        card_code, runtime_desc, slot_index = record
        if any(isinstance(value, bool) or not isinstance(value, int) for value in record):
            raise ValueError("static semantic runtime binding values must be integers")
        if not 0 < card_code <= 0x7FFFFFFF or not 0 < runtime_desc <= 0xFFFFFFFF:
            raise ValueError("static semantic runtime binding identity is invalid")
        if not 0 <= slot_index < SEMANTIC_EFFECT_SLOTS:
            raise ValueError("static semantic runtime binding slot is invalid")
        key = (card_code, runtime_desc)
        if key in bindings:
            raise ValueError("static semantic runtime binding contains a duplicate key")
        bindings[key] = slot_index
    return bindings

# 严格还原卡片到有效 Lua 效果槽的轻量审计目录
def _deserialize_card_slots(records):
    """严格还原卡片到有效 Lua 效果槽的轻量审计目录"""
    if not isinstance(records, dict):
        raise ValueError("static semantic card slots must be an object")
    card_slots = {}
    for raw_card_code, raw_slots in records.items():
        if not str(raw_card_code).isdigit() or not isinstance(raw_slots, list):
            raise ValueError("static semantic card slot record is invalid")
        card_code = int(raw_card_code)
        slots = tuple(int(slot) for slot in raw_slots)
        if (
            card_code <= 0
            or any(isinstance(slot, bool) for slot in raw_slots)
            or tuple(sorted(set(slots))) != slots
            or any(not 0 <= slot < SEMANTIC_EFFECT_SLOTS for slot in slots)
        ):
            raise ValueError("static semantic card slot values are invalid")
        card_slots[card_code] = slots
    return card_slots

# 读取并校验轻量目录身份，不加载约 53 MiB 的模型侧数值表
def _read_compiled_catalog(directory, card_vocabulary):
    """读取并校验轻量目录身份，不加载约 53 MiB 的模型侧数值表"""
    table_path, catalog_path = _compiled_asset_paths(directory)
    for path, size_limit in (
        (table_path, MAX_STATIC_SEMANTIC_TABLE_BYTES),
        (catalog_path, MAX_STATIC_SEMANTIC_CATALOG_BYTES),
    ):
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"static semantic asset is missing: {path.name}")
        if path.stat().st_size <= 0 or path.stat().st_size > size_limit:
            raise ValueError(f"static semantic asset size is invalid: {path.name}")
    with open(catalog_path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("static semantic catalog must be an object")
    if payload.get("format_version") != STATIC_SEMANTIC_LOOKUP_FORMAT_VERSION:
        raise ValueError("static semantic catalog format version mismatch")
    if payload.get("table_file") != STATIC_SEMANTIC_TABLE_FILENAME:
        raise ValueError("static semantic catalog table filename mismatch")
    if payload.get("table_size") != table_path.stat().st_size:
        raise ValueError("static semantic table size does not match its catalog")
    if payload.get("card_vocab_hash") != card_vocabulary.vocabulary_hash:
        raise ValueError("static semantic catalog card vocabulary hash mismatch")
    if payload.get("card_vocab_size") != card_vocabulary.capacity:
        raise ValueError("static semantic catalog card vocabulary capacity mismatch")
    if payload.get("card_vocab_card_count") != card_vocabulary.card_count:
        raise ValueError("static semantic catalog card count mismatch")
    semantic_card_slots = _deserialize_card_slots(
        payload.get("semantic_card_slots")
    )
    runtime_effect_bindings = _deserialize_runtime_bindings(
        payload.get("runtime_effect_bindings")
    )
    if payload.get("public_hint_binding_format") != PUBLIC_HINT_BINDING_FORMAT_VERSION:
        raise ValueError("static semantic public hint catalog needs recompilation")
    hints = deserialize_public_hint_catalog(payload.get("public_hint_bindings"))
    return payload, semantic_card_slots, runtime_effect_bindings, hints

# 限制 NPZ 内层成员与展开体积，拒绝嵌套压缩炸弹和额外载荷
def _validate_static_npz_container(table_path, card_vocabulary):
    """限制 NPZ 内层成员与展开体积，拒绝嵌套压缩炸弹和额外载荷"""
    expected_names = {f"{name}.npy" for name in _COMPILED_ARRAY_NAMES}
    with zipfile.ZipFile(table_path, "r") as archive:
        members = archive.infolist()
        actual_names = {item.filename for item in members}
        if len(members) != len(expected_names) or actual_names != expected_names:
            raise ValueError("static semantic NPZ members are invalid")
        if any(
            item.is_dir()
            or "/" in item.filename
            or "\\" in item.filename
            or item.compress_type != zipfile.ZIP_STORED
            or item.flag_bits & 0x1
            for item in members
        ):
            raise ValueError("static semantic NPZ member encoding is unsafe")
        max_payload_bytes = (
            64 * 1024
            + card_vocabulary.capacity
            * SEMANTIC_EFFECT_SLOTS
            * (
                SEMANTIC_CATEGORY_SLOTS * np.dtype(np.int16).itemsize
                + SEMANTIC_REQUIREMENT_SLOTS * np.dtype(np.int8).itemsize
                + SEMANTIC_RELATION_SLOTS
                * (
                    np.dtype(np.int16).itemsize * 3
                    + np.dtype(np.float16).itemsize
                    + np.dtype(np.int32).itemsize
                )
                + np.dtype(np.int32).itemsize
                + np.dtype(np.bool_).itemsize
            )
            + (card_vocabulary.capacity * SEMANTIC_EFFECT_SLOTS + 1)
            * 512
            * np.dtype(np.float32).itemsize
        )
        if sum(item.file_size for item in members) > max_payload_bytes:
            raise ValueError("static semantic NPZ expanded size exceeds protocol bounds")

class StaticSemanticLookup:
    """保存按卡片 token 对齐的只读静态语义矩阵"""

    # 复用 Core V4 的 __init__ 协议逻辑
    def __init__(
        self,
        *,
        card_vocabulary,
        category_to_index,
        requirement_to_index,
        category,
        requirement,
        setcode,
        number,
        reference,
        race,
        attribute,
        code_index,
        effect_mask,
        code_dictionary,
        semantic_card_slots,
        runtime_effect_bindings,
        public_hint_bindings=None,
        code_encoder=None,
        code_policy=None,
    ):
        self.card_vocabulary = card_vocabulary
        self.category_to_index = dict(category_to_index)
        self.requirement_to_index = dict(requirement_to_index)
        self.category = category
        self.requirement = requirement
        self.setcode = setcode
        self.number = number
        self.reference = reference
        self.race = race
        self.attribute = attribute
        self.code_index = code_index
        self.effect_mask = effect_mask
        self.code_dictionary = code_dictionary
        self.semantic_card_slots = semantic_card_slots
        self.runtime_effect_bindings = runtime_effect_bindings
        self.public_hint_bindings = public_hint_bindings or {}
        self.code_encoder = dict(code_encoder) if code_encoder else None
        self.code_policy = dict(code_policy) if code_policy else None
        self._prefix_hashes = {}

    # 返回参与模型查表和语义身份计算的固定顺序数组
    @property
    def table_arrays(self):
        """返回参与模型查表和语义身份计算的固定顺序数组"""
        return (
            self.category,
            self.requirement,
            self.setcode,
            self.number,
            self.reference,
            self.race,
            self.attribute,
            self.code_index,
            self.effect_mask,
        )

    # 按真实卡密返回旧编码器兼容的八组静态语义视图
    def get_card_semantics(self, card_id):
        """按真实卡密返回旧编码器兼容的八组静态语义视图"""
        token_id = self.card_vocabulary.encode(card_id)
        return (
            self.category[token_id],
            self.requirement[token_id],
            self.setcode[token_id],
            self.number[token_id],
            self.reference[token_id],
            self.race[token_id],
            self.attribute[token_id],
            self.code_index[token_id],
        )

    # 计算指定只追加词表前缀实际可见的逻辑语义指纹
    def prefix_hash(self, card_count=None):
        """计算指定只追加词表前缀实际可见的逻辑语义指纹"""
        if card_count is None:
            card_count = self.card_vocabulary.card_count
        if isinstance(card_count, bool) or not isinstance(card_count, int):
            raise ValueError("semantic lookup card count must be an integer")
        if not 0 <= card_count <= self.card_vocabulary.card_count:
            raise ValueError("semantic lookup card count is outside the vocabulary")
        cached = self._prefix_hashes.get(card_count)
        if cached is not None:
            return cached

        prefix_vocabulary = self.card_vocabulary.prefix(card_count)
        token_limit = FIRST_CARD_TOKEN_ID + card_count
        used_categories = set(
            int(value)
            for value in np.unique(self.category[:token_limit])
            if int(value) > 0
        )
        used_requirements = set(
            int(value)
            for value in np.unique(self.requirement[:token_limit])
            if int(value) >= 0
        )
        category_labels = {
            label: index
            for label, index in self.category_to_index.items()
            if index in used_categories
        }
        requirement_labels = {
            label: index
            for label, index in self.requirement_to_index.items()
            if index in used_requirements
        }
        header = {
            "format_version": STATIC_SEMANTIC_LOOKUP_FORMAT_VERSION,
            "card_count": card_count,
            "card_mapping": prefix_vocabulary.cards,
            "category_labels": category_labels,
            "requirement_labels": requirement_labels,
            "effect_slots": SEMANTIC_EFFECT_SLOTS,
            "code_dimension": int(self.code_dictionary.shape[1]),
            "public_hint_binding_format": PUBLIC_HINT_BINDING_FORMAT_VERSION,
            "public_hint_bindings": serialize_public_hint_catalog({
                desc: record for desc, record in self.public_hint_bindings.items()
                if record[0] in prefix_vocabulary.cards
            }),
        }
        digest = hashlib.sha256(
            json.dumps(
                header,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        for array in self.table_arrays[:-2]:
            digest.update(array.dtype.str.encode("ascii"))
            digest.update(_canonical_array_bytes(array[:token_limit]))
        digest.update(_canonical_array_bytes(self.effect_mask[:token_limit]))

        # code_index 的物理行号可在增量生成时变化；身份按实际槽位向量计算
        for start in range(0, token_limit, 64):
            indices = self.code_index[start : min(start + 64, token_limit)]
            vectors = self.code_dictionary[indices]
            digest.update(_canonical_array_bytes(vectors))

        semantic_hash = digest.hexdigest()
        self._prefix_hashes[card_count] = semantic_hash
        return semantic_hash

_COMPILED_ARRAY_NAMES = (
    "category",
    "requirement",
    "setcode",
    "number",
    "reference",
    "race",
    "attribute",
    "code_index",
    "effect_mask",
    "code_dictionary",
)

# 从独立资产恢复模型查表，并验证数值表、词表和逻辑语义身份
def load_static_semantic_assets(
    directory=".",
    *,
    card_vocabulary=None,
    verify_source_files=False,
    verify_logical_hash=False,
):
    """从独立资产恢复模型查表，并验证数值表、词表和逻辑语义身份"""
    vocabulary = card_vocabulary or get_default_card_vocabulary()
    root = Path(directory).resolve()
    table_path, _ = _compiled_asset_paths(root)
    catalog, semantic_card_slots, runtime_effect_bindings, public_hint_bindings = _read_compiled_catalog(
        root,
        vocabulary,
    )
    if catalog.get("table_sha256") != _sha256_file(table_path):
        raise ValueError("static semantic table hash does not match its catalog")
    if verify_source_files:
        CODE_EMBEDDINGS_META_FILENAME = 'code_embeddings_meta.json'
        source_records = catalog.get("source_files")
        if not isinstance(source_records, dict):
            raise ValueError("static semantic source records are missing")
        source_names = set(source_records)
        if (
            len(source_names - {CODE_EMBEDDINGS_META_FILENAME}) != 3
            or CODE_EMBEDDINGS_FILENAME not in source_names
            or CODE_EMBEDDINGS_INDEX_FILENAME not in source_names
            or any(
                not isinstance(filename, str)
                or filename != Path(filename).name
                or filename in {"", ".", ".."}
                for filename in source_names
            )
        ):
            raise ValueError("static semantic source filename set is invalid")
        for filename, record in source_records.items():
            source_path = root / filename
            if source_path.is_symlink() or not source_path.is_file():
                raise FileNotFoundError(
                    f"static semantic source is missing: {filename}"
                )
            if (
                not isinstance(record, dict)
                or record.get("size") != source_path.stat().st_size
                or record.get("sha256") != _sha256_file(source_path)
            ):
                raise ValueError(
                    "semantic_lookup_hash source mismatch with compiled asset: "
                    f"{filename}"
                )

    expected_arrays = catalog.get("arrays")
    if not isinstance(expected_arrays, dict):
        raise ValueError("static semantic array manifest is missing")
    _validate_static_npz_container(table_path, vocabulary)
    with np.load(table_path, allow_pickle=False) as archive:
        if set(archive.files) != set(_COMPILED_ARRAY_NAMES):
            raise ValueError("static semantic table contains unexpected arrays")
        arrays = {}
        for name in _COMPILED_ARRAY_NAMES:
            array = np.asarray(archive[name])
            record = expected_arrays.get(name)
            if not isinstance(record, dict):
                raise ValueError(f"static semantic array record is missing: {name}")
            if record.get("dtype") != array.dtype.str or record.get("shape") != list(
                array.shape
            ):
                raise ValueError(f"static semantic array shape mismatch: {name}")
            if array.dtype.hasobject:
                raise ValueError(f"static semantic object array is not allowed: {name}")
            arrays[name] = np.array(array, copy=True)

    capacity = vocabulary.capacity
    expected_shapes = {
        "category": (capacity, SEMANTIC_EFFECT_SLOTS, SEMANTIC_CATEGORY_SLOTS),
        "requirement": (
            capacity,
            SEMANTIC_EFFECT_SLOTS,
            SEMANTIC_REQUIREMENT_SLOTS,
        ),
        "setcode": (capacity, SEMANTIC_EFFECT_SLOTS, SEMANTIC_RELATION_SLOTS),
        "number": (capacity, SEMANTIC_EFFECT_SLOTS, SEMANTIC_RELATION_SLOTS),
        "reference": (capacity, SEMANTIC_EFFECT_SLOTS, SEMANTIC_RELATION_SLOTS),
        "race": (capacity, SEMANTIC_EFFECT_SLOTS, SEMANTIC_RELATION_SLOTS),
        "attribute": (capacity, SEMANTIC_EFFECT_SLOTS, SEMANTIC_RELATION_SLOTS),
        "code_index": (capacity, SEMANTIC_EFFECT_SLOTS),
        "effect_mask": (capacity, SEMANTIC_EFFECT_SLOTS),
    }
    expected_dtypes = {
        "category": np.dtype(np.int16),
        "requirement": np.dtype(np.int8),
        "setcode": np.dtype(np.int16),
        "number": np.dtype(np.float16),
        "reference": np.dtype(np.int32),
        "race": np.dtype(np.int16),
        "attribute": np.dtype(np.int16),
        "code_index": np.dtype(np.int32),
        "effect_mask": np.dtype(np.bool_),
    }
    for name, shape in expected_shapes.items():
        if arrays[name].shape != shape or arrays[name].dtype != expected_dtypes[name]:
            raise ValueError(f"static semantic array contract mismatch: {name}")
    code_dictionary = arrays["code_dictionary"]
    if (
        code_dictionary.ndim != 2
        or code_dictionary.dtype != np.float32
        or not 1 <= code_dictionary.shape[1] <= 512
        or code_dictionary.shape[0]
        > capacity * SEMANTIC_EFFECT_SLOTS + 1
    ):
        raise ValueError("static semantic code dictionary is invalid")
    if arrays["code_index"].size and (
        arrays["code_index"].min() < 0
        or arrays["code_index"].max() >= code_dictionary.shape[0]
    ):
        raise ValueError("static semantic code index exceeds its dictionary")

    category_to_index = catalog.get("category_to_index")
    requirement_to_index = catalog.get("requirement_to_index")
    if not isinstance(category_to_index, dict) or not isinstance(
        requirement_to_index, dict
    ):
        raise ValueError("static semantic label vocabularies are invalid")
    for mapping, lower_bound, upper_bound, label in (
        (
            category_to_index,
            0,
            SEMANTIC_CATEGORY_CAPACITY,
            "category",
        ),
        (
            requirement_to_index,
            0,
            SEMANTIC_REQUIREMENT_CAPACITY,
            "requirement",
        ),
    ):
        values = list(mapping.values())
        if (
            any(not isinstance(key, str) or not key for key in mapping)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not lower_bound <= value < upper_bound
                for value in values
            )
            or len(values) != len(set(values))
        ):
            raise ValueError(f"static semantic {label} vocabulary is invalid")
    if category_to_index.get("<PAD>") != 0 or category_to_index.get("<UNK>") != 1:
        raise ValueError("static semantic category reserved indices are invalid")
    bounded_arrays = (
        (arrays["category"], 0, SEMANTIC_CATEGORY_CAPACITY, "category"),
        (arrays["requirement"], -1, SEMANTIC_REQUIREMENT_CAPACITY, "requirement"),
        (arrays["setcode"], 0, 4096, "setcode"),
        (arrays["reference"], 0, capacity, "reference"),
        (arrays["race"], 0, 30, "race"),
        (arrays["attribute"], 0, 10, "attribute"),
    )
    for array, lower_bound, upper_bound, label in bounded_arrays:
        if array.size and (
            array.min() < lower_bound or array.max() >= upper_bound
        ):
            raise ValueError(f"static semantic {label} values are outside bounds")
    if not np.isfinite(arrays["number"]).all() or not np.isfinite(
        code_dictionary
    ).all():
        raise ValueError("static semantic floating-point values must be finite")
    contract = catalog.get("code_encoder_contract") or {}
    if not isinstance(contract, dict) or any(value is not None and not isinstance(value, dict)
            for value in (contract.get("encoder"), contract.get("policy"))):
        raise ValueError("invalid static semantic encoder contract")
    if not contract.get("encoder"):
        from .semantic_compatibility import read_semantic_compatibility_registry
        registry = read_semantic_compatibility_registry(root)
        descriptor = registry["bases"].get(catalog.get("semantic_lookup_hash")) if registry else None
        if descriptor:
            contract = {"encoder": descriptor.get("encoder"), "policy": descriptor.get("policy")}
    lookup = StaticSemanticLookup(
        card_vocabulary=vocabulary,
        category_to_index=category_to_index,
        requirement_to_index=requirement_to_index,
        category=arrays["category"],
        requirement=arrays["requirement"],
        setcode=arrays["setcode"],
        number=arrays["number"],
        reference=arrays["reference"],
        race=arrays["race"],
        attribute=arrays["attribute"],
        code_index=arrays["code_index"],
        effect_mask=arrays["effect_mask"],
        code_dictionary=code_dictionary,
        semantic_card_slots=semantic_card_slots,
        runtime_effect_bindings=runtime_effect_bindings,
        public_hint_bindings=public_hint_bindings,
        code_encoder=contract.get("encoder"),
        code_policy=contract.get("policy"),
    )
    if catalog.get("semantic_lookup_card_count") != vocabulary.card_count:
        raise ValueError("static semantic lookup card count mismatch")
    logical_hash = catalog.get("semantic_lookup_hash")
    if (
        not isinstance(logical_hash, str)
        or len(logical_hash) != 64
        or any(char not in "0123456789abcdef" for char in logical_hash)
    ):
        raise ValueError("static semantic logical hash is invalid")
    if verify_logical_hash:
        if logical_hash != lookup.prefix_hash():
            raise ValueError("static semantic logical hash does not match its table")
    else:
        # 表文件摘要已验证，正常启动直接复用编译时逻辑身份，避免重复扫描向量
        lookup._prefix_hashes[vocabulary.card_count] = logical_hash
    return lookup

# 读取显式绑定的编译语义且禁止自动重建
# 复用 Core V4 的 get_static_semantic_lookup 协议逻辑
def get_static_semantic_lookup(directory=".", *, card_vocabulary=None, **kwargs):
    from .runtime_context import current_assets
    assets = current_assets()
    return assets.lookup

# 编码器通过推理上下文使用轻量目录而不修改进程全局目录
# 复用 Core V4 的 register_static_semantic_runtime_catalog 协议逻辑
def register_static_semantic_runtime_catalog(**kwargs):
    return get_static_semantic_lookup().runtime_effect_bindings
