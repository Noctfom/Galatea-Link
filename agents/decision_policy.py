# 决策介入策略模块，负责选择 Core、LLM 或规则兜底并携带决策元数据

from dataclasses import dataclass
from typing import Any

from app_config import DecisionConfig


@dataclass(frozen=True)
class DecisionOutcome:
    response: Any
    source: str
    choice_id: int | None = None
    reason: str = ""
    chat_message: str | None = None
    core_confidence: float | None = None


class InterventionPolicy:
    # 初始化介入模式与阈值配置
    def __init__(self, config: DecisionConfig):
        self.config = config

    # 判断当前模式是否需要先运行 Core
    def should_run_core(self) -> bool:
        return self.config.mode != "llm_only"

    # 根据模式、时点和 Core 置信度判断是否调用 LLM
    def should_use_llm(self, msg_type: int, core_decision) -> bool:
        if self.config.mode == "core_only":
            return False
        if self.config.mode in {"llm_only", "llm_review"}:
            return True
        if msg_type in self.config.force_llm_message_types:
            return True
        if core_decision is None:
            return True
        return core_decision.confidence < self.config.core_confidence_threshold

    # 将 Core 建议添加到 LLM 可见观察副本
    def attach_core_suggestion(self, observation: dict, core_decision) -> dict:
        if not self.config.include_core_suggestion or core_decision is None:
            return observation
        observation["core_suggestion"] = {
            "choice_id": core_decision.choice_id,
            "confidence": core_decision.confidence,
            "probability_margin": core_decision.probability_margin,
        }
        return observation
