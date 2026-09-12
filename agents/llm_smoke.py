# LLM 冒烟测试模块，使用最小合法观察验证接口延迟和结构化动作输出

from __future__ import annotations

import time
from typing import Any

from agents.llm_client import create_llm_client
from app_config import LlmConfig


# 构建不包含真实对局和卡组信息的最小合法观察
def build_llm_smoke_observation() -> dict[str, Any]:
    return {
        "schema_version": "galatea.llm_observation.v1",
        "information_quality": {
            "scope": "api_smoke_test",
            "complete_protocol_projection": False,
            "limitations": ["当前是 API 冒烟测试而不是真实对局"],
        },
        "event": {"type": 13, "name": "select_yes_no"},
        "turn": {
            "number": 1,
            "phase_id": 4,
            "phase_name": "主要阶段1",
            "actor": "self",
        },
        "players": {
            "self": {"lp": 8000, "hand_count": 5},
            "opponent": {"lp": 8000, "hand_count": 5},
        },
        "cards": [],
        "known_information": {},
        "chain": [],
        "recent_history": [],
        "decision_required": True,
        "legal_actions": [
            {
                "choice_id": 0,
                "action_type": 13,
                "action_name": "answer_yes_no",
                "description": "No",
            },
            {
                "choice_id": 1,
                "action_type": 13,
                "action_name": "answer_yes_no",
                "description": "Yes",
            },
        ],
        "card_catalog": [],
    }


# 执行一次真实但输入规模受限的 LLM API 请求
async def run_llm_smoke_test(config: LlmConfig) -> dict[str, Any]:
    client = create_llm_client(config)
    if client is None:
        raise RuntimeError("请先启用 LLM 配置")
    started_at = time.perf_counter()
    try:
        decision = await client.decide(build_llm_smoke_observation())
    finally:
        await client.close()
    return {
        "schema_version": "galatea.link.llm_test.v1",
        "ok": True,
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "model": config.model,
        "choice_id": decision.choice_id,
        "reason": decision.reason,
        "chat_message": decision.chat_message,
    }
