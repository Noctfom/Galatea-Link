# V4 只读精确词表及前缀校验，固定复用 Core V4 协议且不依赖外部 Core 目录

import hashlib
import json
from pathlib import Path

CARD_VOCAB_FILENAME = "card_vocab.json"

DEFAULT_CARD_VOCAB_CAPACITY = 20000

CARD_VOCAB_FORMAT_VERSION = 1

FIRST_CARD_TOKEN_ID = 10

CARD_VOCAB_RESERVED_TOKENS = {
    "PAD": 0,
    "UNK": 1,
    "HIDDEN_HAND": 2,
    "HIDDEN_SZONE": 3,
}

MAX_CARD_VOCAB_FILE_BYTES = 16 * 1024 * 1024

# 生成不受 JSON 排版影响的词表指纹载荷
def _canonical_mapping_payload(capacity, cards):
    """生成不受 JSON 排版影响的词表指纹载荷"""
    return {
        "format_version": CARD_VOCAB_FORMAT_VERSION,
        "capacity": int(capacity),
        "first_card_token_id": FIRST_CARD_TOKEN_ID,
        "reserved_tokens": CARD_VOCAB_RESERVED_TOKENS,
        "cards": {str(code): int(token_id) for code, token_id in cards.items()},
    }

# 计算模型与资产共同校验的稳定词表哈希
def calculate_card_vocab_hash(capacity, cards):
    """计算模型与资产共同校验的稳定词表哈希"""
    payload = _canonical_mapping_payload(capacity, cards)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

class CardVocabulary:
    """保存经过严格校验的精确卡片索引"""

    # 复用 Core V4 的 __init__ 协议逻辑
    def __init__(self, cards, *, capacity=DEFAULT_CARD_VOCAB_CAPACITY):
        normalized = {}
        for raw_code, raw_token_id in cards.items():
            if isinstance(raw_code, bool) or isinstance(raw_token_id, bool):
                raise ValueError("card vocabulary entries must be integer pairs")
            try:
                code = int(raw_code)
                token_id = int(raw_token_id)
            except (TypeError, ValueError) as error:
                raise ValueError("card vocabulary entries must be integer pairs") from error
            if code <= 0 or str(code) != str(raw_code):
                raise ValueError(f"invalid canonical card code: {raw_code!r}")
            if code in normalized:
                raise ValueError(f"duplicate card code in vocabulary: {code}")
            normalized[code] = token_id

        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise ValueError("card vocabulary capacity must be an integer")
        if capacity <= FIRST_CARD_TOKEN_ID:
            raise ValueError("card vocabulary capacity is too small")
        ordered = sorted(normalized.items(), key=lambda item: item[1])
        expected_ids = list(range(FIRST_CARD_TOKEN_ID, FIRST_CARD_TOKEN_ID + len(ordered)))
        actual_ids = [token_id for _, token_id in ordered]
        if actual_ids != expected_ids:
            raise ValueError("card token ids must be unique and contiguous from index 10")
        if actual_ids and actual_ids[-1] >= capacity:
            raise ValueError("card vocabulary exceeds the configured embedding capacity")

        self.capacity = capacity
        self.cards = dict(ordered)
        self.card_count = len(self.cards)
        self.vocabulary_hash = calculate_card_vocab_hash(capacity, self.cards)

    # 复用 Core V4 的 padding_id 协议逻辑
    @property
    def padding_id(self):
        return CARD_VOCAB_RESERVED_TOKENS["PAD"]

    # 复用 Core V4 的 unknown_id 协议逻辑
    @property
    def unknown_id(self):
        return CARD_VOCAB_RESERVED_TOKENS["UNK"]

    # 复用 Core V4 的 hidden_hand_id 协议逻辑
    @property
    def hidden_hand_id(self):
        return CARD_VOCAB_RESERVED_TOKENS["HIDDEN_HAND"]

    # 复用 Core V4 的 hidden_szone_id 协议逻辑
    @property
    def hidden_szone_id(self):
        return CARD_VOCAB_RESERVED_TOKENS["HIDDEN_SZONE"]

    # 把真实卡片代码转换为唯一索引，未收录代码统一返回 UNK
    def encode(self, code):
        """把真实卡片代码转换为唯一索引，未收录代码统一返回 UNK"""
        try:
            normalized = int(code)
        except (TypeError, ValueError):
            return self.unknown_id
        return self.cards.get(normalized, self.unknown_id)

    # 判断真实卡片代码是否已经被精确词表收录
    def contains(self, code):
        """判断真实卡片代码是否已经被精确词表收录"""
        try:
            return int(code) in self.cards
        except (TypeError, ValueError):
            return False

    # 截取指定长度的只追加前缀，用于校验旧 V4 产物
    def prefix(self, card_count):
        """截取指定长度的只追加前缀，用于校验旧 V4 产物"""
        if isinstance(card_count, bool) or not isinstance(card_count, int):
            raise ValueError("card vocabulary prefix length must be an integer")
        if card_count < 0 or card_count > self.card_count:
            raise ValueError("card vocabulary prefix length is outside the mapping")
        return CardVocabulary(
            dict(list(self.cards.items())[:card_count]),
            capacity=self.capacity,
        )

    # 生成可写入磁盘且自带完整性信息的词表对象
    def to_payload(self, *, source_cdb_sha256=None):
        """生成可写入磁盘且自带完整性信息的词表对象"""
        payload = _canonical_mapping_payload(self.capacity, self.cards)
        payload.update(
            {
                "card_count": self.card_count,
                "vocabulary_sha256": self.vocabulary_hash,
                "source_cdb_sha256": source_cdb_sha256,
            }
        )
        return payload

# 从普通 JSON 文件加载并严格校验精确卡片词表
def load_card_vocabulary(path=CARD_VOCAB_FILENAME):
    """从普通 JSON 文件加载并严格校验精确卡片词表"""
    vocabulary_path = Path(path)
    if vocabulary_path.is_symlink() or not vocabulary_path.is_file():
        raise FileNotFoundError(f"card vocabulary does not exist: {vocabulary_path}")
    if vocabulary_path.stat().st_size > MAX_CARD_VOCAB_FILE_BYTES:
        raise ValueError("card vocabulary exceeds the 16 MiB safety limit")
    with open(vocabulary_path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("card vocabulary must be a JSON object")
    if payload.get("format_version") != CARD_VOCAB_FORMAT_VERSION:
        raise ValueError("card vocabulary format version is incompatible")
    if payload.get("first_card_token_id") != FIRST_CARD_TOKEN_ID:
        raise ValueError("card vocabulary first token id is incompatible")
    if payload.get("reserved_tokens") != CARD_VOCAB_RESERVED_TOKENS:
        raise ValueError("card vocabulary reserved tokens are incompatible")
    cards = payload.get("cards")
    if not isinstance(cards, dict):
        raise ValueError("card vocabulary cards must be a JSON object")
    vocabulary = CardVocabulary(cards, capacity=payload.get("capacity"))
    if payload.get("card_count") != vocabulary.card_count:
        raise ValueError("card vocabulary card count does not match its mapping")
    if payload.get("vocabulary_sha256") != vocabulary.vocabulary_hash:
        raise ValueError("card vocabulary hash does not match its mapping")
    source_hash = payload.get("source_cdb_sha256")
    if source_hash is not None and (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(char not in "0123456789abcdef" for char in source_hash)
    ):
        raise ValueError("card vocabulary source CDB hash is invalid")
    return vocabulary

# 读取当前推理上下文的精确词表
# 复用 Core V4 的 get_default_card_vocabulary 协议逻辑
def get_default_card_vocabulary():
    from .runtime_context import current_assets
    return current_assets().vocabulary
