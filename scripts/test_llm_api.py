# LLM API 独立冒烟脚本，负责脱离游戏连接验证延迟和结构化动作输出

import asyncio
import argparse
import os
import sys
from pathlib import Path

# 允许直接运行脚本时导入项目根目录模块
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from agents.llm_client import create_llm_client
from app_config import load_app_config
from core.llm_prompt_context import build_llm_static_context
from utils.deck_utils import load_deck


# 解析冒烟次数和请求间隔参数
def parse_args():
    parser = argparse.ArgumentParser(description="独立验证 LLM API 和缓存命中")
    parser.add_argument("--repeat", type=int, default=1, help="重复请求次数")
    parser.add_argument("--interval", type=float, default=2.0, help="重复请求间隔秒数")
    return parser.parse_args()


# 构建不包含真实对局信息的最小合法观察
def build_smoke_observation():
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


# 加载项目配置并执行一次真实 LLM 请求
async def main():
    args = parse_args()
    if args.repeat < 1:
        raise ValueError("--repeat 必须大于 0")
    config = load_app_config(PROJECT_ROOT / "config.yaml")
    client = create_llm_client(config.llm)
    if client is None:
        raise RuntimeError("请先在 config.yaml 中启用 llm.enabled")
    deck = load_deck(PROJECT_ROOT / "decks", config.agent.deck)
    if deck is None:
        raise FileNotFoundError(f"找不到卡组文件: {config.agent.deck}")
    client.set_static_context(
        build_llm_static_context(
            deck.main,
            deck.extra,
            deck_name=deck.name,
            agent_name=config.agent.name,
            include_card_text=config.llm.cache_deck_text,
        )
    )
    try:
        for attempt in range(1, args.repeat + 1):
            decision = await client.decide(build_smoke_observation())
            print(
                f"✅ LLM API 冒烟成功 [{attempt}/{args.repeat}] "
                f"choice_id={decision.choice_id} "
                f"reason={decision.reason!r} "
                f"chat_message={decision.chat_message!r}"
            )
            if attempt < args.repeat and args.interval > 0:
                await asyncio.sleep(args.interval)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
