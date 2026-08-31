# 对局外部运行时接口模块，负责状态读取、事件订阅和动态介入控制

import asyncio
import copy
from dataclasses import replace
from typing import Any, Iterable

from core.link_events import LinkEvent, LinkEventBus, LinkEventSubscription


VALID_INTERVENTION_MODES = {"core_only", "llm_only", "llm_review", "hybrid"}


class GalateaRuntimeApi:
    # 初始化与具体社交平台解耦的运行时接口
    def __init__(self, link: Any, event_bus: LinkEventBus):
        self._link = link
        self._event_bus = event_bus
        self._control_lock = asyncio.Lock()

    # 创建供 AstrBot 或其他消费者读取的事件订阅
    def subscribe_events(self, max_queue_size: int = 128) -> LinkEventSubscription:
        return self._event_bus.subscribe(max_queue_size=max_queue_size)

    # 返回不包含密钥和网络载荷的当前运行状态
    def get_status(self) -> dict[str, Any]:
        observation = self._link.get_latest_llm_observation()
        decision_config = self._link.decision_policy.config
        coordinator = getattr(self._link, "decision_coordinator", None)
        return {
            "schema_version": "galatea.runtime_status.v1",
            "connected": bool(getattr(self._link.client, "is_connected", False)),
            "duel_active": bool(getattr(self._link, "duel_active", False)),
            "player_id": getattr(self._link, "ai_player_id", None),
            "decision": {
                "mode": decision_config.mode,
                "core_confidence_threshold": decision_config.core_confidence_threshold,
                "force_llm_message_types": list(
                    decision_config.force_llm_message_types
                ),
                "active_request_id": getattr(
                    coordinator,
                    "active_request_id",
                    None,
                ),
                "last_source": getattr(self._link, "last_decision_source", None),
                "last_choice_id": getattr(
                    self._link,
                    "last_decision_choice_id",
                    None,
                ),
            },
            "latest_observation_id": (
                observation.get("observation_id") if observation else None
            ),
            "latest_chat_suggestion": getattr(
                self._link,
                "latest_chat_suggestion",
                None,
            ),
        }

    # 返回当前标准可见观察的独立副本
    def get_latest_observation(self) -> dict[str, Any] | None:
        return self._link.get_latest_llm_observation()

    # 原子更新运行中的 LLM 介入模式和置信度条件
    async def update_intervention(
        self,
        *,
        mode: str | None = None,
        core_confidence_threshold: float | None = None,
        force_llm_message_types: Iterable[int] | None = None,
    ) -> dict[str, Any]:
        async with self._control_lock:
            current = self._link.decision_policy.config
            next_mode = current.mode if mode is None else mode
            if next_mode not in VALID_INTERVENTION_MODES:
                raise ValueError(f"不支持的介入模式: {next_mode}")

            next_threshold = (
                current.core_confidence_threshold
                if core_confidence_threshold is None
                else float(core_confidence_threshold)
            )
            if isinstance(core_confidence_threshold, bool):
                raise ValueError("Core 置信度阈值不能是布尔值")
            if not 0.0 <= next_threshold <= 1.0:
                raise ValueError("Core 置信度阈值必须位于 0 到 1")

            next_force_types = current.force_llm_message_types
            if force_llm_message_types is not None:
                next_force_types = self._normalize_message_types(
                    force_llm_message_types
                )

            updated = replace(
                current,
                mode=next_mode,
                core_confidence_threshold=next_threshold,
                force_llm_message_types=next_force_types,
            )
            self._link.decision_policy.config = updated
            payload = self._decision_config_payload(updated)
            self._event_bus.publish("runtime.intervention.updated", payload)
            return copy.deepcopy(payload)

    # 将社交平台消息投递到对局运行时事件流
    async def publish_external_message(
        self,
        text: str,
        *,
        source: str,
        sender_id: str | None = None,
    ) -> LinkEvent | None:
        normalized_text = str(text).strip()
        normalized_source = str(source).strip()
        if not normalized_text:
            raise ValueError("外部消息不能为空")
        if len(normalized_text) > 2000:
            raise ValueError("外部消息不能超过 2000 个字符")
        if not normalized_source:
            raise ValueError("外部消息来源不能为空")
        return self._event_bus.publish(
            "external.message.received",
            {
                "text": normalized_text,
                "source": normalized_source,
                "sender_id": str(sender_id) if sender_id is not None else None,
            },
        )

    # 校验并规范化强制介入的 OCG 消息类型
    def _normalize_message_types(self, values: Iterable[int]) -> tuple[int, ...]:
        result = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("强制介入消息类型必须是整数")
            if not 0 <= value <= 0xFF:
                raise ValueError("强制介入消息类型必须位于 0 到 255")
            if value not in result:
                result.append(value)
        return tuple(result)

    # 构建可公开返回的介入配置副本
    def _decision_config_payload(self, config) -> dict[str, Any]:
        return {
            "mode": config.mode,
            "core_confidence_threshold": config.core_confidence_threshold,
            "force_llm_message_types": list(config.force_llm_message_types),
            "include_core_suggestion": config.include_core_suggestion,
            "llm_time_budget": config.llm_time_budget,
        }
