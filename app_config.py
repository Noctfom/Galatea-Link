# Galatea Link 配置加载模块，负责解析服务端、智能体和模型设置

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ServerConfig:
    profile: str = "mdpro3"
    host: str = "127.0.0.1"
    port: int = 7911
    password: str = ""
    protocol_version: int = 0x1361
    game_id: int = 0
    connect_timeout: float = 10.0
    trace_packets: bool = False


@dataclass(frozen=True)
class AgentConfig:
    name: str = "Galatea_AI"
    deck: str = "神秘白龙"
    prefer_second: bool = False


@dataclass(frozen=True)
class ModelConfig:
    device: str = "cpu"
    weights_path: str = "./models/galatea_iter_110.pth"
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LlmConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "GALATEA_LLM_API_KEY"
    model: str = ""
    timeout: float = 30.0
    max_tokens: int = 512
    temperature: float = 0.1
    response_format: str = "json_object"
    trace_requests: bool = True
    thinking_mode: str = "auto"
    cache_static_context: bool = True
    cache_deck_text: bool = False
    compact_dynamic_observation: bool = True


@dataclass(frozen=True)
class DecisionConfig:
    mode: str = "core_only"
    core_confidence_threshold: float = 0.65
    force_llm_message_types: tuple[int, ...] = ()
    include_core_suggestion: bool = True
    llm_time_budget: float = 12.0
    autonomous_intervention_enabled: bool = False
    autonomous_allowed_modes: tuple[str, ...] = (
        "core_only",
        "hybrid",
        "llm_review",
        "llm_only",
    )
    autonomous_core_confidence_min: float = 0.2
    autonomous_core_confidence_max: float = 0.9
    autonomous_max_ttl_decisions: int = 3
    autonomous_max_force_message_types: int = 8


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)


# 校验配置段并转换为普通映射
def _as_mapping(value: Any, section: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"配置段 {section} 必须是键值映射")
    return value


# 解析支持十进制和十六进制的整数配置
def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"配置项 {field_name} 不能是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as exc:
            raise ValueError(f"配置项 {field_name} 必须是整数") from exc
    raise ValueError(f"配置项 {field_name} 必须是整数")


# 解析布尔配置并拒绝含义不明的字符串
def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(f"配置项 {field_name} 必须是布尔值")


# 解析消息类型整数列表
def _as_int_tuple(value: Any, field_name: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"配置项 {field_name} 必须是整数列表")
    return tuple(_as_int(item, field_name) for item in value)


# 解析去重后的非空字符串列表
def _as_str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"配置项 {field_name} 必须是字符串列表")
    result = []
    for item in value:
        normalized = str(item).strip()
        if not normalized:
            raise ValueError(f"配置项 {field_name} 不能包含空字符串")
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


# 从配置映射构建不可变的应用配置
def build_app_config(raw_config: Mapping[str, Any] | None) -> AppConfig:
    root = _as_mapping(raw_config, "root")
    server_raw = _as_mapping(root.get("server"), "server")
    agent_raw = _as_mapping(root.get("agent"), "agent")
    model_raw = _as_mapping(root.get("model"), "model")
    llm_raw = _as_mapping(root.get("llm"), "llm")
    decision_raw = _as_mapping(root.get("decision"), "decision")
    net_raw = _as_mapping(model_raw.get("config"), "model.config")

    server = ServerConfig(
        profile=str(server_raw.get("profile", "mdpro3")),
        host=str(server_raw.get("host", "127.0.0.1")),
        port=_as_int(server_raw.get("port", 7911), "server.port"),
        password=str(server_raw.get("password", "")),
        protocol_version=_as_int(
            server_raw.get("protocol_version", 0x1361),
            "server.protocol_version",
        ),
        game_id=_as_int(server_raw.get("game_id", 0), "server.game_id"),
        connect_timeout=float(server_raw.get("connect_timeout", 10.0)),
        trace_packets=_as_bool(
            server_raw.get("trace_packets", False),
            "server.trace_packets",
        ),
    )
    agent = AgentConfig(
        name=str(agent_raw.get("name", "Galatea_AI")),
        deck=str(agent_raw.get("deck", "神秘白龙")),
        prefer_second=_as_bool(
            agent_raw.get("prefer_second", False),
            "agent.prefer_second",
        ),
    )
    model = ModelConfig(
        device=str(model_raw.get("device", "cpu")),
        weights_path=str(
            model_raw.get("weights_path", "./models/galatea_iter_110.pth")
        ),
        config=dict(net_raw),
    )
    response_format = str(llm_raw.get("response_format", "json_object"))
    if response_format not in {"json_object", "json_schema", "none"}:
        raise ValueError("配置项 llm.response_format 不受支持")
    thinking_mode = str(llm_raw.get("thinking_mode", "auto"))
    if thinking_mode not in {"auto", "enabled", "disabled"}:
        raise ValueError("配置项 llm.thinking_mode 不受支持")
    llm = LlmConfig(
        enabled=_as_bool(llm_raw.get("enabled", False), "llm.enabled"),
        provider=str(llm_raw.get("provider", "openai_compatible")),
        base_url=str(llm_raw.get("base_url", "https://api.openai.com/v1")),
        api_key=str(llm_raw.get("api_key", "")),
        api_key_env=str(llm_raw.get("api_key_env", "GALATEA_LLM_API_KEY")),
        model=str(llm_raw.get("model", "")),
        timeout=float(llm_raw.get("timeout", 30.0)),
        max_tokens=_as_int(llm_raw.get("max_tokens", 512), "llm.max_tokens"),
        temperature=float(llm_raw.get("temperature", 0.1)),
        response_format=response_format,
        trace_requests=_as_bool(
            llm_raw.get("trace_requests", True),
            "llm.trace_requests",
        ),
        thinking_mode=thinking_mode,
        cache_static_context=_as_bool(
            llm_raw.get("cache_static_context", True),
            "llm.cache_static_context",
        ),
        cache_deck_text=_as_bool(
            llm_raw.get("cache_deck_text", False),
            "llm.cache_deck_text",
        ),
        compact_dynamic_observation=_as_bool(
            llm_raw.get("compact_dynamic_observation", True),
            "llm.compact_dynamic_observation",
        ),
    )
    decision_mode = str(decision_raw.get("mode", "core_only"))
    if decision_mode not in {"core_only", "llm_only", "llm_review", "hybrid"}:
        raise ValueError("配置项 decision.mode 不受支持")
    confidence_threshold = float(
        decision_raw.get("core_confidence_threshold", 0.65)
    )
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError("配置项 decision.core_confidence_threshold 必须位于 0 到 1")
    decision = DecisionConfig(
        mode=decision_mode,
        core_confidence_threshold=confidence_threshold,
        force_llm_message_types=_as_int_tuple(
            decision_raw.get("force_llm_message_types", []),
            "decision.force_llm_message_types",
        ),
        include_core_suggestion=_as_bool(
            decision_raw.get("include_core_suggestion", True),
            "decision.include_core_suggestion",
        ),
        llm_time_budget=float(decision_raw.get("llm_time_budget", 12.0)),
        autonomous_intervention_enabled=_as_bool(
            decision_raw.get("autonomous_intervention_enabled", False),
            "decision.autonomous_intervention_enabled",
        ),
        autonomous_allowed_modes=_as_str_tuple(
            decision_raw.get(
                "autonomous_allowed_modes",
                ["core_only", "hybrid", "llm_review", "llm_only"],
            ),
            "decision.autonomous_allowed_modes",
        ),
        autonomous_core_confidence_min=float(
            decision_raw.get("autonomous_core_confidence_min", 0.2)
        ),
        autonomous_core_confidence_max=float(
            decision_raw.get("autonomous_core_confidence_max", 0.9)
        ),
        autonomous_max_ttl_decisions=_as_int(
            decision_raw.get("autonomous_max_ttl_decisions", 3),
            "decision.autonomous_max_ttl_decisions",
        ),
        autonomous_max_force_message_types=_as_int(
            decision_raw.get("autonomous_max_force_message_types", 8),
            "decision.autonomous_max_force_message_types",
        ),
    )
    if decision.llm_time_budget <= 0:
        raise ValueError("配置项 decision.llm_time_budget 必须大于 0")
    invalid_autonomous_modes = set(decision.autonomous_allowed_modes) - {
        "core_only",
        "llm_only",
        "llm_review",
        "hybrid",
    }
    if not decision.autonomous_allowed_modes or invalid_autonomous_modes:
        raise ValueError("配置项 decision.autonomous_allowed_modes 包含无效模式")
    if not (
        0.0
        <= decision.autonomous_core_confidence_min
        <= decision.autonomous_core_confidence_max
        <= 1.0
    ):
        raise ValueError("自主介入置信度范围必须位于 0 到 1 且最小值不大于最大值")
    if decision.autonomous_max_ttl_decisions < 1:
        raise ValueError("配置项 decision.autonomous_max_ttl_decisions 必须大于 0")
    if decision.autonomous_max_force_message_types < 0:
        raise ValueError(
            "配置项 decision.autonomous_max_force_message_types 不能小于 0"
        )
    return AppConfig(
        server=server,
        agent=agent,
        model=model,
        llm=llm,
        decision=decision,
    )


# 从 YAML 文件加载应用配置
def load_app_config(path: str | Path = "config.yaml") -> AppConfig:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("缺少 PyYAML 依赖，无法读取 config.yaml") from exc

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as config_file:
        raw_config = yaml.safe_load(config_file) or {}
    return build_app_config(raw_config)
