# 运行时控制纯数据模块，统一校验在线与离线策略设置

from dataclasses import replace
from typing import Any, Iterable, Mapping


VALID_INTERVENTION_MODES = {"core_only", "llm_only", "llm_review", "hybrid"}
VALID_CORE_POLICY_MODES = {"greedy", "deployment"}
VALID_AGENT_BACKENDS = {"local", "remote_astrbot"}


# 校验并规范化强制介入的 OCG 消息类型
def normalize_message_types(values: Iterable[int]) -> tuple[int, ...]:
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
def normalize_modes(values: Iterable[str]) -> tuple[str, ...]:
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


# 校验决策控制配置的范围和依赖关系
def validate_decision_controls(config: Any) -> None:
    if config.mode not in VALID_INTERVENTION_MODES:
        raise ValueError(f"不支持的介入模式: {config.mode}")
    if config.agent_backend not in VALID_AGENT_BACKENDS:
        raise ValueError(f"不支持的智能体后端: {config.agent_backend}")
    if config.core_policy_mode not in VALID_CORE_POLICY_MODES:
        raise ValueError(f"不支持的 Core 动作策略: {config.core_policy_mode}")
    if not 0.05 <= config.core_temperature <= 5.0:
        raise ValueError("Core 模型温度必须位于 0.05 到 5.0")
    if not 0.0 <= config.core_confidence_threshold <= 1.0:
        raise ValueError("Core 置信度阈值必须位于 0 到 1")
    if config.core_time_budget <= 0:
        raise ValueError("Core 时间预算必须大于 0")
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


# 校验聊天控制配置的范围和自动发送限制
def validate_game_chat_controls(config: Any) -> None:
    if not 1 <= config.max_context_messages <= 100:
        raise ValueError("聊天上下文消息数必须位于 1 到 100")
    if not 1 <= config.max_context_chars <= 16000:
        raise ValueError("聊天上下文字符数必须位于 1 到 16000")
    if not 1 <= config.max_outbound_utf16_units <= 255:
        raise ValueError("聊天发送长度必须位于 1 到 255")
    if config.min_auto_send_interval < 0:
        raise ValueError("自动发言间隔不能小于 0")


# 从统一控制补丁构建新的决策基线配置
def apply_decision_control_patch(config: Any, patch: Mapping[str, Any]) -> Any:
    unknown_sections = set(patch) - {"intervention", "autonomy", "game_chat"}
    if unknown_sections:
        raise ValueError(f"不支持的宏观控制分区: {sorted(unknown_sections)}")
    intervention = patch.get("intervention", {})
    autonomy = patch.get("autonomy", {})
    if not isinstance(intervention, Mapping) or not isinstance(autonomy, Mapping):
        raise ValueError("宏观控制分区必须是映射")

    allowed_intervention = {
        "mode",
        "agent_backend",
        "core_policy_mode",
        "core_temperature",
        "core_confidence_threshold",
        "force_llm_message_types",
        "include_core_suggestion",
        "core_time_budget",
        "llm_time_budget",
    }
    allowed_autonomy = {
        "enabled",
        "allowed_modes",
        "core_confidence_min",
        "core_confidence_max",
        "max_ttl_decisions",
        "max_force_message_types",
    }
    if set(intervention) - allowed_intervention or set(autonomy) - allowed_autonomy:
        raise ValueError("宏观控制补丁包含未知字段")

    replacements = {}
    if "mode" in intervention:
        replacements["mode"] = str(intervention["mode"])
    if "agent_backend" in intervention:
        replacements["agent_backend"] = str(intervention["agent_backend"]).strip().casefold()
    if "core_policy_mode" in intervention:
        replacements["core_policy_mode"] = str(intervention["core_policy_mode"])
    if "core_temperature" in intervention:
        value = intervention["core_temperature"]
        if isinstance(value, bool):
            raise ValueError("Core 模型温度不能是布尔值")
        replacements["core_temperature"] = float(value)
    if "core_confidence_threshold" in intervention:
        value = intervention["core_confidence_threshold"]
        if isinstance(value, bool):
            raise ValueError("Core 置信度阈值不能是布尔值")
        replacements["core_confidence_threshold"] = float(value)
    if "force_llm_message_types" in intervention:
        replacements["force_llm_message_types"] = normalize_message_types(
            intervention["force_llm_message_types"]
        )
    if "include_core_suggestion" in intervention:
        value = intervention["include_core_suggestion"]
        if not isinstance(value, bool):
            raise ValueError("include_core_suggestion 必须是布尔值")
        replacements["include_core_suggestion"] = value
    if "core_time_budget" in intervention:
        value = intervention["core_time_budget"]
        if isinstance(value, bool):
            raise ValueError("Core 时间预算不能是布尔值")
        replacements["core_time_budget"] = float(value)
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
        replacements["autonomous_allowed_modes"] = normalize_modes(
            autonomy["allowed_modes"]
        )

    updated = replace(config, **replacements)
    validate_decision_controls(updated)
    return updated


# 从统一控制补丁构建新的聊天基线配置
def apply_game_chat_control_patch(config: Any, patch: Mapping[str, Any]) -> Any:
    game_chat = patch.get("game_chat", {})
    if not isinstance(game_chat, Mapping):
        raise ValueError("游戏聊天控制分区必须是映射")
    allowed_fields = {
        "enabled",
        "capture_incoming",
        "send_enabled",
        "include_in_llm_context",
        "llm_suggestions_enabled",
        "auto_send_llm_chat",
        "max_context_messages",
        "max_context_chars",
        "max_outbound_utf16_units",
        "min_auto_send_interval",
    }
    if set(game_chat) - allowed_fields:
        raise ValueError("游戏聊天控制补丁包含未知字段")

    replacements = {}
    boolean_fields = {
        "enabled",
        "capture_incoming",
        "send_enabled",
        "include_in_llm_context",
        "llm_suggestions_enabled",
        "auto_send_llm_chat",
    }
    for field_name in boolean_fields:
        if field_name not in game_chat:
            continue
        value = game_chat[field_name]
        if not isinstance(value, bool):
            raise ValueError(f"{field_name} 必须是布尔值")
        replacements[field_name] = value
    for field_name in {
        "max_context_messages",
        "max_context_chars",
        "max_outbound_utf16_units",
    }:
        if field_name not in game_chat:
            continue
        value = game_chat[field_name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_name} 必须是整数")
        replacements[field_name] = value
    if "min_auto_send_interval" in game_chat:
        value = game_chat["min_auto_send_interval"]
        if isinstance(value, bool):
            raise ValueError("min_auto_send_interval 不能是布尔值")
        replacements["min_auto_send_interval"] = float(value)

    updated = replace(config, **replacements)
    validate_game_chat_controls(updated)
    return updated


# 构建可公开返回的介入策略副本
def intervention_payload(config: Any) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "agent_backend": config.agent_backend,
        "core_policy_mode": config.core_policy_mode,
        "core_temperature": config.core_temperature,
        "core_confidence_threshold": config.core_confidence_threshold,
        "force_llm_message_types": list(config.force_llm_message_types),
        "include_core_suggestion": config.include_core_suggestion,
        "core_time_budget": config.core_time_budget,
        "llm_time_budget": config.llm_time_budget,
    }


# 构建可公开返回的游戏聊天设置副本
def game_chat_payload(config: Any) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "capture_incoming": config.capture_incoming,
        "send_enabled": config.send_enabled,
        "include_in_llm_context": config.include_in_llm_context,
        "llm_suggestions_enabled": config.llm_suggestions_enabled,
        "auto_send_llm_chat": config.auto_send_llm_chat,
        "max_context_messages": config.max_context_messages,
        "max_context_chars": config.max_context_chars,
        "max_outbound_utf16_units": config.max_outbound_utf16_units,
        "min_auto_send_interval": config.min_auto_send_interval,
    }


# 构建在线和离线共用的完整控制面快照
def build_controls_snapshot(
    decision_config: Any,
    game_chat_config: Any,
    revision: int,
    *,
    effective_config: Any | None = None,
    active_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective = effective_config or decision_config
    return {
        "schema_version": "galatea.runtime_controls.v1",
        "revision": int(revision),
        "intervention": intervention_payload(effective),
        "baseline_intervention": intervention_payload(decision_config),
        "autonomy": {
            "enabled": decision_config.autonomous_intervention_enabled,
            "allowed_modes": list(decision_config.autonomous_allowed_modes),
            "core_confidence_min": decision_config.autonomous_core_confidence_min,
            "core_confidence_max": decision_config.autonomous_core_confidence_max,
            "max_ttl_decisions": decision_config.autonomous_max_ttl_decisions,
            "max_force_message_types": (
                decision_config.autonomous_max_force_message_types
            ),
            "active_override": active_override,
        },
        "game_chat": game_chat_payload(game_chat_config),
    }
