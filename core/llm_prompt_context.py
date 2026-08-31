# LLM 固定提示上下文模块，负责将卡组知识整理为可缓存前缀

from collections import Counter
from typing import Any

from utils.card_reader import card_db


# 清除卡密中的引擎标记位
def _pure_code(raw_code: int) -> int:
    return int(raw_code or 0) & 0x7FFFFFFF


# 按卡密汇总卡组并按配置附加效果文本
def _build_card_entries(codes, card_reader, include_card_text: bool) -> list[dict[str, Any]]:
    counts = Counter(_pure_code(code) for code in codes if _pure_code(code))
    entries = []
    for code in sorted(counts):
        entry = {
            "code": code,
            "name": card_reader.get_card_name(code),
            "count": counts[code],
        }
        if include_card_text:
            text = card_reader.get_card_text(code)
            if text:
                entry["text"] = text
        entries.append(entry)
    return entries


# 构建本局内保持不变的卡组与观察约定
def build_llm_static_context(
    main_deck,
    extra_deck,
    *,
    deck_name: str,
    agent_name: str,
    include_card_text: bool = False,
    card_reader=card_db,
) -> dict[str, Any]:
    return {
        "schema_version": "galatea.llm_static_context.v1",
        "agent": {"name": agent_name},
        "own_initial_deck": {
            "name": deck_name,
            "main": _build_card_entries(main_deck, card_reader, include_card_text),
            "extra": _build_card_entries(extra_deck, card_reader, include_card_text),
        },
        "observation_contract": {
            "visibility": "后续动态观察只包含当前玩家依法可见的信息",
            "remaining_cards": "动态观察中的剩余卡组使用 code 和 count 表示并以此处名称为准",
            "card_catalog": "若此处已有某卡效果文本 后续动态 card_catalog 可省略该卡",
            "unknown_information": "没有出现在动态观察中的对手隐藏身份必须视为未知",
        },
    }
