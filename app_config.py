# Galatea Link 配置加载模块，负责解析服务端、智能体和模型设置

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


LINK_PROJECT_ROOT = Path(__file__).resolve().parent


# 将相对资源路径稳定解析到 Link 项目目录
def resolve_link_resource_path(path: str) -> str:
    resource_path = Path(path).expanduser()
    if not resource_path.is_absolute():
        resource_path = LINK_PROJECT_ROOT / resource_path
    return str(resource_path.resolve())


@dataclass(frozen=True)
class ServerConfig:
    profile: str = "ygopro"
    host: str = "127.0.0.1"
    port: int = 7911
    password: str = ""
    protocol_version: int = 0x1361
    auto_negotiate_version: bool = True
    max_version_retries: int = 1
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
    weights_path: str = "./models/galatea_iter_30.pth"
    expected_model_id: str = ""
    protocol: str = "auto"
    inference_backend: str = "auto"
    onnx_providers: tuple[str, ...] = ("CPUExecutionProvider",)
    onnx_intra_op_threads: int = 0
    assets_path: str = ""
    strict_asset_hashes: bool = True
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
    agent_backend: str = "local"
    core_policy_mode: str = "greedy"
    core_temperature: float = 0.8
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
class GameChatConfig:
    enabled: bool = True
    capture_incoming: bool = True
    send_enabled: bool = True
    include_in_llm_context: bool = True
    llm_suggestions_enabled: bool = True
    auto_send_llm_chat: bool = False
    max_context_messages: int = 12
    max_context_chars: int = 3000
    max_outbound_utf16_units: int = 120
    min_auto_send_interval: float = 15.0


@dataclass(frozen=True)
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    api_token: str = ""
    api_token_env: str = "GALATEA_LINK_API_TOKEN"
    event_queue_size: int = 256
    webui_enabled: bool = True
    allowed_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    game_chat: GameChatConfig = field(default_factory=GameChatConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)


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
    game_chat_raw = _as_mapping(root.get("game_chat"), "game_chat")
    service_raw = _as_mapping(root.get("service"), "service")
    net_raw = _as_mapping(model_raw.get("config"), "model.config")

    server = ServerConfig(
        profile=str(server_raw.get("profile", "ygopro")),
        host=str(server_raw.get("host", "127.0.0.1")),
        port=_as_int(server_raw.get("port", 7911), "server.port"),
        password=str(server_raw.get("password", "")),
        protocol_version=_as_int(
            server_raw.get("protocol_version", 0x1361),
            "server.protocol_version",
        ),
        auto_negotiate_version=_as_bool(
            server_raw.get("auto_negotiate_version", True),
            "server.auto_negotiate_version",
        ),
        max_version_retries=_as_int(
            server_raw.get("max_version_retries", 1),
            "server.max_version_retries",
        ),
        game_id=_as_int(server_raw.get("game_id", 0), "server.game_id"),
        connect_timeout=float(server_raw.get("connect_timeout", 10.0)),
        trace_packets=_as_bool(
            server_raw.get("trace_packets", False),
            "server.trace_packets",
        ),
    )
    if not 0 <= server.max_version_retries <= 3:
        raise ValueError("配置项 server.max_version_retries 必须位于 0 到 3")
    agent = AgentConfig(
        name=str(agent_raw.get("name", "Galatea_AI")),
        deck=str(agent_raw.get("deck", "神秘白龙")),
        prefer_second=_as_bool(
            agent_raw.get("prefer_second", False),
            "agent.prefer_second",
        ),
    )
    model_protocol = str(model_raw.get("protocol", "auto"))
    if model_protocol.strip().casefold() not in {
        "auto",
        "1",
        "v1",
        "legacy",
        "core-3.4.2",
        "3",
        "v3",
        "core-3.6.3",
        "core-3.6.5",
    }:
        raise ValueError("配置项 model.protocol 不受支持")
    inference_backend = str(
        model_raw.get("inference_backend", "auto")
    ).strip().casefold()
    if inference_backend not in {"auto", "pytorch", "onnxruntime"}:
        raise ValueError("配置项 model.inference_backend 不受支持")
    onnx_providers = _as_str_tuple(
        model_raw.get("onnx_providers", ["CPUExecutionProvider"]),
        "model.onnx_providers",
    )
    if not onnx_providers:
        raise ValueError("配置项 model.onnx_providers 不能为空")
    onnx_intra_op_threads = _as_int(
        model_raw.get("onnx_intra_op_threads", 0),
        "model.onnx_intra_op_threads",
    )
    if onnx_intra_op_threads < 0:
        raise ValueError("配置项 model.onnx_intra_op_threads 不能小于 0")
    model = ModelConfig(
        device=str(model_raw.get("device", "cpu")),
        weights_path=str(
            model_raw.get("weights_path", "./models/galatea_iter_30.pth")
        ),
        expected_model_id=str(model_raw.get("expected_model_id", "")),
        protocol=model_protocol,
        inference_backend=inference_backend,
        onnx_providers=onnx_providers,
        onnx_intra_op_threads=onnx_intra_op_threads,
        assets_path=str(model_raw.get("assets_path", "")),
        strict_asset_hashes=_as_bool(
            model_raw.get("strict_asset_hashes", True),
            "model.strict_asset_hashes",
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
    agent_backend = str(decision_raw.get("agent_backend", "local")).strip().casefold()
    if agent_backend not in {"local", "remote_astrbot"}:
        raise ValueError("配置项 decision.agent_backend 不受支持")
    confidence_threshold = float(
        decision_raw.get("core_confidence_threshold", 0.65)
    )
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError("配置项 decision.core_confidence_threshold 必须位于 0 到 1")
    core_policy_mode = str(decision_raw.get("core_policy_mode", "greedy"))
    if core_policy_mode not in {"greedy", "deployment"}:
        raise ValueError("配置项 decision.core_policy_mode 不受支持")
    core_temperature = float(decision_raw.get("core_temperature", 0.8))
    if not 0.05 <= core_temperature <= 5.0:
        raise ValueError("配置项 decision.core_temperature 必须位于 0.05 到 5.0")
    decision = DecisionConfig(
        mode=decision_mode,
        agent_backend=agent_backend,
        core_policy_mode=core_policy_mode,
        core_temperature=core_temperature,
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
    game_chat = GameChatConfig(
        enabled=_as_bool(
            game_chat_raw.get("enabled", True),
            "game_chat.enabled",
        ),
        capture_incoming=_as_bool(
            game_chat_raw.get("capture_incoming", True),
            "game_chat.capture_incoming",
        ),
        send_enabled=_as_bool(
            game_chat_raw.get("send_enabled", True),
            "game_chat.send_enabled",
        ),
        include_in_llm_context=_as_bool(
            game_chat_raw.get("include_in_llm_context", True),
            "game_chat.include_in_llm_context",
        ),
        llm_suggestions_enabled=_as_bool(
            game_chat_raw.get("llm_suggestions_enabled", True),
            "game_chat.llm_suggestions_enabled",
        ),
        auto_send_llm_chat=_as_bool(
            game_chat_raw.get("auto_send_llm_chat", False),
            "game_chat.auto_send_llm_chat",
        ),
        max_context_messages=_as_int(
            game_chat_raw.get("max_context_messages", 12),
            "game_chat.max_context_messages",
        ),
        max_context_chars=_as_int(
            game_chat_raw.get("max_context_chars", 3000),
            "game_chat.max_context_chars",
        ),
        max_outbound_utf16_units=_as_int(
            game_chat_raw.get("max_outbound_utf16_units", 120),
            "game_chat.max_outbound_utf16_units",
        ),
        min_auto_send_interval=float(
            game_chat_raw.get("min_auto_send_interval", 15.0)
        ),
    )
    if not 1 <= game_chat.max_context_messages <= 100:
        raise ValueError("配置项 game_chat.max_context_messages 必须位于 1 到 100")
    if not 1 <= game_chat.max_context_chars <= 16000:
        raise ValueError("配置项 game_chat.max_context_chars 必须位于 1 到 16000")
    if not 1 <= game_chat.max_outbound_utf16_units <= 255:
        raise ValueError(
            "配置项 game_chat.max_outbound_utf16_units 必须位于 1 到 255"
        )
    if game_chat.min_auto_send_interval < 0:
        raise ValueError("配置项 game_chat.min_auto_send_interval 不能小于 0")
    service = ServiceConfig(
        host=str(service_raw.get("host", "127.0.0.1")).strip(),
        port=_as_int(service_raw.get("port", 8765), "service.port"),
        api_token=str(service_raw.get("api_token", "")),
        api_token_env=str(
            service_raw.get("api_token_env", "GALATEA_LINK_API_TOKEN")
        ).strip(),
        event_queue_size=_as_int(
            service_raw.get("event_queue_size", 256),
            "service.event_queue_size",
        ),
        webui_enabled=_as_bool(
            service_raw.get("webui_enabled", True),
            "service.webui_enabled",
        ),
        allowed_origins=_as_str_tuple(
            service_raw.get("allowed_origins", []),
            "service.allowed_origins",
        ),
    )
    if not service.host:
        raise ValueError("配置项 service.host 不能为空")
    if not 1 <= service.port <= 65535:
        raise ValueError("配置项 service.port 必须位于 1 到 65535")
    if not 8 <= service.event_queue_size <= 4096:
        raise ValueError("配置项 service.event_queue_size 必须位于 8 到 4096")
    return AppConfig(
        server=server,
        agent=agent,
        model=model,
        llm=llm,
        decision=decision,
        game_chat=game_chat,
        service=service,
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
