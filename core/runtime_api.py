# 对局外部运行时接口模块，负责状态读取、事件订阅和动态介入控制

import asyncio
import copy
from dataclasses import replace
from typing import Any, Iterable, Mapping

from core.link_events import LinkEvent, LinkEventBus, LinkEventSubscription


VALID_INTERVENTION_MODES = {"core_only", "llm_only", "llm_review", "hybrid"}


class GalateaRuntimeApi:
    # 初始化与具体社交平台解耦的运行时接口
    def __init__(self, link: Any, event_bus: LinkEventBus):
        self._link = link
        self._event_bus = event_bus
        self._control_lock = asyncio.Lock()
        self._baseline_config = link.decision_policy.config
        self._revision = 0
        self._active_autonomous_override: dict[str, Any] | None = None

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
                "controls_revision": self._revision,
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

    # 返回前端和 AstrBot 共用的完整宏观控制状态
    def get_controls(self) -> dict[str, Any]:
        effective = self._link.decision_policy.config
        baseline = self._baseline_config
        return {
            "schema_version": "galatea.runtime_controls.v1",
            "revision": self._revision,
            "intervention": self._intervention_payload(effective),
            "baseline_intervention": self._intervention_payload(baseline),
            "autonomy": {
                "enabled": baseline.autonomous_intervention_enabled,
                "allowed_modes": list(baseline.autonomous_allowed_modes),
                "core_confidence_min": (
                    baseline.autonomous_core_confidence_min
                ),
                "core_confidence_max": (
                    baseline.autonomous_core_confidence_max
                ),
                "max_ttl_decisions": baseline.autonomous_max_ttl_decisions,
                "max_force_message_types": (
                    baseline.autonomous_max_force_message_types
                ),
                "active_override": copy.deepcopy(
                    self._active_autonomous_override
                ),
            },
        }

    # 通过单一补丁接口原子更新全部宏观控制并取消旧自主覆盖
    async def update_controls(
        self,
        patch: Mapping[str, Any],
        *,
        source: str = "external",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(patch, Mapping) or not patch:
            raise ValueError("宏观控制补丁必须是非空映射")
        normalized_source = str(source).strip()
        if not normalized_source:
            raise ValueError("宏观控制来源不能为空")

        async with self._control_lock:
            if expected_revision is not None and expected_revision != self._revision:
                raise RuntimeError(
                    f"宏观控制版本冲突 当前为 {self._revision} 请求为 {expected_revision}"
                )
            previous = self.get_controls()
            updated = self._apply_controls_patch(self._baseline_config, patch)
            self._baseline_config = updated
            self._active_autonomous_override = None
            self._link.decision_policy.config = updated
            self._revision += 1
            current = self.get_controls()
            self._event_bus.publish(
                "runtime.controls.updated",
                {
                    "source": normalized_source,
                    "previous_revision": previous["revision"],
                    "controls": current,
                },
            )
            return copy.deepcopy(current)

    # 原子更新运行中的 LLM 介入模式和置信度条件
    async def update_intervention(
        self,
        *,
        mode: str | None = None,
        core_confidence_threshold: float | None = None,
        force_llm_message_types: Iterable[int] | None = None,
    ) -> dict[str, Any]:
        intervention_patch = {}
        if mode is not None:
            intervention_patch["mode"] = mode
        if core_confidence_threshold is not None:
            intervention_patch["core_confidence_threshold"] = (
                core_confidence_threshold
            )
        if force_llm_message_types is not None:
            intervention_patch["force_llm_message_types"] = list(
                force_llm_message_types
            )
        controls = await self.update_controls(
            {"intervention": intervention_patch},
            source="external.intervention",
        )
        return copy.deepcopy(controls["intervention"])

    # 应用 LLM 建议的临时介入覆盖并严格执行人工护栏
    async def apply_autonomous_intervention(
        self,
        update: Any,
        *,
        request_id: int,
    ) -> bool:
        async with self._control_lock:
            baseline = self._baseline_config
            if not baseline.autonomous_intervention_enabled:
                self._publish_autonomy_ignored(request_id, "自主介入开关未启用")
                return False
            base_revision = getattr(update, "base_revision", None)
            if base_revision != self._revision:
                self._publish_autonomy_ignored(
                    request_id,
                    f"自主介入控制版本已过期 当前为 {self._revision} 建议基于 {base_revision}",
                )
                return False
            if self._active_autonomous_override is not None:
                self._publish_autonomy_ignored(
                    request_id,
                    "已有自主介入覆盖仍在有效期内",
                )
                return False
            try:
                replacements = self._validate_autonomous_update(update, baseline)
            except ValueError as error:
                self._publish_autonomy_ignored(request_id, str(error))
                return False

            ttl_decisions = min(
                int(getattr(update, "ttl_decisions", 1)),
                baseline.autonomous_max_ttl_decisions,
            )
            effective = replace(baseline, **replacements)
            self._link.decision_policy.config = effective
            self._active_autonomous_override = {
                "origin_request_id": request_id,
                "remaining_decisions": ttl_decisions,
                "reason": str(getattr(update, "reason", ""))[:500],
            }
            self._revision += 1
            self._event_bus.publish(
                "runtime.controls.updated",
                {
                    "source": "llm_supervisor",
                    "request_id": request_id,
                    "controls": self.get_controls(),
                },
            )
            return True

    # 在每次动作提交后推进自主覆盖寿命并按时恢复人工基线
    async def on_decision_committed(self, request_id: int) -> None:
        async with self._control_lock:
            active = self._active_autonomous_override
            if active is None:
                return
            remaining = int(active["remaining_decisions"]) - 1
            if remaining > 0:
                active["remaining_decisions"] = remaining
                self._event_bus.publish(
                    "runtime.autonomy.progressed",
                    {
                        "request_id": request_id,
                        "remaining_decisions": remaining,
                    },
                )
                return

            expired = copy.deepcopy(active)
            self._active_autonomous_override = None
            self._link.decision_policy.config = self._baseline_config
            self._revision += 1
            self._event_bus.publish(
                "runtime.controls.updated",
                {
                    "source": "llm_supervisor_expired",
                    "request_id": request_id,
                    "expired_override": expired,
                    "controls": self.get_controls(),
                },
            )

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

    # 规范化并校验允许自主切换的介入模式
    def _normalize_modes(self, values: Iterable[str]) -> tuple[str, ...]:
        result = []
        for value in values:
            mode = str(value).strip()
            if mode not in VALID_INTERVENTION_MODES:
                raise ValueError(f"不支持的介入模式: {mode}")
            if mode not in result:
                result.append(mode)
        if not result:
            raise ValueError("允许的自主介入模式不能为空")
        return tuple(result)

    # 从统一控制补丁构建新的人工基线配置
    def _apply_controls_patch(self, config, patch: Mapping[str, Any]):
        unknown_sections = set(patch) - {"intervention", "autonomy"}
        if unknown_sections:
            raise ValueError(f"不支持的宏观控制分区: {sorted(unknown_sections)}")
        intervention = patch.get("intervention", {})
        autonomy = patch.get("autonomy", {})
        if not isinstance(intervention, Mapping) or not isinstance(autonomy, Mapping):
            raise ValueError("宏观控制分区必须是映射")

        unknown_intervention = set(intervention) - {
            "mode",
            "core_confidence_threshold",
            "force_llm_message_types",
            "include_core_suggestion",
            "llm_time_budget",
        }
        unknown_autonomy = set(autonomy) - {
            "enabled",
            "allowed_modes",
            "core_confidence_min",
            "core_confidence_max",
            "max_ttl_decisions",
            "max_force_message_types",
        }
        if unknown_intervention or unknown_autonomy:
            raise ValueError("宏观控制补丁包含未知字段")

        replacements = {}
        if "mode" in intervention:
            mode = str(intervention["mode"])
            if mode not in VALID_INTERVENTION_MODES:
                raise ValueError(f"不支持的介入模式: {mode}")
            replacements["mode"] = mode
        if "core_confidence_threshold" in intervention:
            value = intervention["core_confidence_threshold"]
            if isinstance(value, bool):
                raise ValueError("Core 置信度阈值不能是布尔值")
            replacements["core_confidence_threshold"] = float(value)
        if "force_llm_message_types" in intervention:
            replacements["force_llm_message_types"] = self._normalize_message_types(
                intervention["force_llm_message_types"]
            )
        if "include_core_suggestion" in intervention:
            value = intervention["include_core_suggestion"]
            if not isinstance(value, bool):
                raise ValueError("include_core_suggestion 必须是布尔值")
            replacements["include_core_suggestion"] = value
        if "llm_time_budget" in intervention:
            value = intervention["llm_time_budget"]
            if isinstance(value, bool):
                raise ValueError("LLM 时间预算不能是布尔值")
            replacements["llm_time_budget"] = float(value)

        autonomy_fields = {
            "enabled": "autonomous_intervention_enabled",
            "core_confidence_min": "autonomous_core_confidence_min",
            "core_confidence_max": "autonomous_core_confidence_max",
            "max_ttl_decisions": "autonomous_max_ttl_decisions",
            "max_force_message_types": "autonomous_max_force_message_types",
        }
        if "enabled" in autonomy and not isinstance(autonomy["enabled"], bool):
            raise ValueError("自主介入开关必须是布尔值")
        for patch_name, field_name in autonomy_fields.items():
            if patch_name not in autonomy:
                continue
            value = autonomy[patch_name]
            if patch_name == "enabled":
                replacements[field_name] = value
            elif patch_name in {"max_ttl_decisions", "max_force_message_types"}:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{patch_name} 必须是整数")
                replacements[field_name] = value
            else:
                if isinstance(value, bool):
                    raise ValueError(f"{patch_name} 不能是布尔值")
                replacements[field_name] = float(value)
        if "allowed_modes" in autonomy:
            replacements["autonomous_allowed_modes"] = self._normalize_modes(
                autonomy["allowed_modes"]
            )

        updated = replace(config, **replacements)
        self._validate_controls_config(updated)
        return updated

    # 校验统一控制配置内部的范围和依赖关系
    def _validate_controls_config(self, config) -> None:
        if not 0.0 <= config.core_confidence_threshold <= 1.0:
            raise ValueError("Core 置信度阈值必须位于 0 到 1")
        if config.llm_time_budget <= 0:
            raise ValueError("LLM 时间预算必须大于 0")
        if not (
            0.0
            <= config.autonomous_core_confidence_min
            <= config.autonomous_core_confidence_max
            <= 1.0
        ):
            raise ValueError("自主介入置信度范围无效")
        if config.autonomous_max_ttl_decisions < 1:
            raise ValueError("自主介入 TTL 必须大于 0")
        if config.autonomous_max_force_message_types < 0:
            raise ValueError("自主强制时点数量不能小于 0")

    # 校验 LLM 自主调整并转换为可替换配置字段
    def _validate_autonomous_update(self, update: Any, baseline) -> dict[str, Any]:
        replacements = {}
        mode = getattr(update, "mode", None)
        if mode is not None:
            if mode not in baseline.autonomous_allowed_modes:
                raise ValueError(f"自主介入模式不在允许范围: {mode}")
            replacements["mode"] = mode

        threshold = getattr(update, "core_confidence_threshold", None)
        if threshold is not None:
            if isinstance(threshold, bool) or not (
                baseline.autonomous_core_confidence_min
                <= float(threshold)
                <= baseline.autonomous_core_confidence_max
            ):
                raise ValueError("自主介入置信度超出人工护栏")
            replacements["core_confidence_threshold"] = float(threshold)

        force_types = getattr(update, "force_llm_message_types", None)
        if force_types is not None:
            normalized = self._normalize_message_types(force_types)
            if len(normalized) > baseline.autonomous_max_force_message_types:
                raise ValueError("自主强制时点数量超出人工护栏")
            replacements["force_llm_message_types"] = normalized

        ttl_decisions = getattr(update, "ttl_decisions", 1)
        if isinstance(ttl_decisions, bool) or not isinstance(ttl_decisions, int):
            raise ValueError("自主介入 TTL 必须是整数")
        if ttl_decisions < 1:
            raise ValueError("自主介入 TTL 必须大于 0")
        if not replacements:
            raise ValueError("自主介入没有提供可应用的调整")
        return replacements

    # 发布被开关或护栏拒绝的自主调整事件
    def _publish_autonomy_ignored(self, request_id: int, reason: str) -> None:
        self._event_bus.publish(
            "runtime.autonomy.ignored",
            {"request_id": request_id, "reason": reason},
        )

    # 构建可公开返回的介入配置副本
    def _intervention_payload(self, config) -> dict[str, Any]:
        return {
            "mode": config.mode,
            "core_confidence_threshold": config.core_confidence_threshold,
            "force_llm_message_types": list(config.force_llm_message_types),
            "include_core_suggestion": config.include_core_suggestion,
            "llm_time_budget": config.llm_time_budget,
        }
