# Link 可编辑配置模块，校验非敏感设置并生成脱敏控制台快照

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import replace
from typing import Any, Mapping

from app_config import AgentConfig, LlmConfig, ServerConfig, ServiceConfig


ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
VALID_RESPONSE_FORMATS = {"json_object", "json_schema", "none"}
VALID_THINKING_MODES = {"auto", "enabled", "disabled"}


# 读取严格布尔字段并拒绝数字和字符串代替
def require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是布尔值")
    return value


# 读取严格整数字段并支持十六进制文本
def require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError as error:
            raise ValueError(f"{field_name} 必须是整数") from error
    raise ValueError(f"{field_name} 必须是整数")


# 读取有限浮点字段并拒绝布尔值
def require_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是数字")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} 必须是数字") from error
    if result != result or result in {float("inf"), float("-inf")}:
        raise ValueError(f"{field_name} 必须是有限数字")
    return result


# 校验 WebUI 可修改的服务器、Agent 和 LLM 配置
def validate_editable_configuration(
    server: ServerConfig,
    agent: AgentConfig,
    llm: LlmConfig,
) -> None:
    if server.profile.strip().casefold() not in {"ygopro", "mdpro3"}:
        raise ValueError("当前只启用 YGOPro 兼容服务器配置")
    if not server.host.strip() or len(server.host) > 255 or "\x00" in server.host:
        raise ValueError("YGOPro 服务器地址无效")
    if not 1 <= server.port <= 65535:
        raise ValueError("YGOPro 服务器端口必须位于 1 到 65535")
    if not 0 <= server.protocol_version <= 0xFFFFFFFF:
        raise ValueError("游戏协议版本必须位于 0 到 0xFFFFFFFF")
    if not 0 <= server.max_version_retries <= 3:
        raise ValueError("协议版本自动重试次数必须位于 0 到 3")
    if not 0 <= server.game_id <= 0xFFFFFFFF:
        raise ValueError("房间编号必须位于 0 到 0xFFFFFFFF")
    if not 0.5 <= server.connect_timeout <= 120.0:
        raise ValueError("游戏连接超时必须位于 0.5 到 120 秒")

    name = agent.name.strip()
    if not name or "\x00" in name or len(name.encode("utf-16le")) // 2 > 20:
        raise ValueError("Agent 名称必须为 1 到 20 个 UTF-16 编码单元")
    deck = agent.deck.strip()
    if (
        not deck
        or len(deck) > 128
        or "\x00" in deck
        or "/" in deck
        or "\\" in deck
        or ".." in deck
    ):
        raise ValueError("卡组名称无效")

    if llm.provider != "openai_compatible":
        raise ValueError("当前只支持 openai_compatible LLM 提供方")
    parsed_url = urllib.parse.urlsplit(llm.base_url.strip())
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise ValueError("LLM Base URL 必须是无内嵌凭据的 HTTP 或 HTTPS 地址")
    if len(llm.base_url) > 2048:
        raise ValueError("LLM Base URL 过长")
    if llm.enabled and not llm.model.strip():
        raise ValueError("启用 LLM 时必须填写模型名称")
    if len(llm.model) > 255:
        raise ValueError("LLM 模型名称过长")
    if not 0.5 <= llm.timeout <= 300.0:
        raise ValueError("LLM 请求超时必须位于 0.5 到 300 秒")
    if not 1 <= llm.max_tokens <= 32768:
        raise ValueError("LLM 最大输出 Token 必须位于 1 到 32768")
    if not 0.0 <= llm.temperature <= 2.0:
        raise ValueError("LLM 温度必须位于 0 到 2")
    if llm.response_format not in VALID_RESPONSE_FORMATS:
        raise ValueError("LLM 结构化输出模式不受支持")
    if llm.thinking_mode not in VALID_THINKING_MODES:
        raise ValueError("LLM 思考模式不受支持")
    if llm.api_key_env and not ENVIRONMENT_NAME_PATTERN.fullmatch(llm.api_key_env):
        raise ValueError("LLM 密钥环境变量名称无效")


# 应用 WebUI 配置补丁并返回新的不可变配置对象
def apply_configuration_patch(
    server: ServerConfig,
    agent: AgentConfig,
    llm: LlmConfig,
    patch: Mapping[str, Any],
) -> tuple[ServerConfig, AgentConfig, LlmConfig]:
    unknown_sections = set(patch) - {"server", "agent", "llm"}
    if unknown_sections:
        raise ValueError(f"配置补丁包含未知分区: {sorted(unknown_sections)}")
    server_patch = patch.get("server", {})
    agent_patch = patch.get("agent", {})
    llm_patch = patch.get("llm", {})
    if not all(isinstance(item, Mapping) for item in (server_patch, agent_patch, llm_patch)):
        raise ValueError("配置补丁分区必须是 JSON 对象")

    server_allowed = {
        "profile",
        "host",
        "port",
        "protocol_version",
        "auto_negotiate_version",
        "max_version_retries",
        "game_id",
        "connect_timeout",
        "trace_packets",
    }
    agent_allowed = {"name", "deck", "prefer_second"}
    llm_allowed = {
        "enabled",
        "provider",
        "base_url",
        "api_key_env",
        "model",
        "timeout",
        "max_tokens",
        "temperature",
        "response_format",
        "trace_requests",
        "thinking_mode",
        "cache_static_context",
        "cache_deck_text",
        "compact_dynamic_observation",
    }
    if set(server_patch) - server_allowed:
        raise ValueError("服务器配置补丁包含未知字段")
    if set(agent_patch) - agent_allowed:
        raise ValueError("Agent 配置补丁包含未知字段")
    if set(llm_patch) - llm_allowed:
        raise ValueError("LLM 配置补丁包含未知字段")

    server_values = {}
    for field_name in {"profile", "host"}:
        if field_name in server_patch:
            server_values[field_name] = str(server_patch[field_name]).strip()
    for field_name in {"port", "protocol_version", "max_version_retries", "game_id"}:
        if field_name in server_patch:
            server_values[field_name] = require_int(
                server_patch[field_name],
                f"server.{field_name}",
            )
    if "connect_timeout" in server_patch:
        server_values["connect_timeout"] = require_float(
            server_patch["connect_timeout"],
            "server.connect_timeout",
        )
    if "trace_packets" in server_patch:
        server_values["trace_packets"] = require_bool(
            server_patch["trace_packets"],
            "server.trace_packets",
        )
    if "auto_negotiate_version" in server_patch:
        server_values["auto_negotiate_version"] = require_bool(
            server_patch["auto_negotiate_version"],
            "server.auto_negotiate_version",
        )

    agent_values = {}
    for field_name in {"name", "deck"}:
        if field_name in agent_patch:
            agent_values[field_name] = str(agent_patch[field_name]).strip()
    if "prefer_second" in agent_patch:
        agent_values["prefer_second"] = require_bool(
            agent_patch["prefer_second"],
            "agent.prefer_second",
        )

    llm_values = {}
    for field_name in {
        "provider",
        "base_url",
        "api_key_env",
        "model",
        "response_format",
        "thinking_mode",
    }:
        if field_name in llm_patch:
            llm_values[field_name] = str(llm_patch[field_name]).strip()
    for field_name in {
        "enabled",
        "trace_requests",
        "cache_static_context",
        "cache_deck_text",
        "compact_dynamic_observation",
    }:
        if field_name in llm_patch:
            llm_values[field_name] = require_bool(
                llm_patch[field_name],
                f"llm.{field_name}",
            )
    if "timeout" in llm_patch:
        llm_values["timeout"] = require_float(llm_patch["timeout"], "llm.timeout")
    if "temperature" in llm_patch:
        llm_values["temperature"] = require_float(
            llm_patch["temperature"],
            "llm.temperature",
        )
    if "max_tokens" in llm_patch:
        llm_values["max_tokens"] = require_int(
            llm_patch["max_tokens"],
            "llm.max_tokens",
        )

    updated_server = replace(server, **server_values)
    updated_agent = replace(agent, **agent_values)
    updated_llm = replace(llm, **llm_values)
    validate_editable_configuration(updated_server, updated_agent, updated_llm)
    return updated_server, updated_agent, updated_llm


# 返回不包含服务器密码和 LLM 密钥的配置快照
def build_configuration_snapshot(
    server: ServerConfig,
    agent: AgentConfig,
    llm: LlmConfig,
    service: ServiceConfig,
    revision: int,
) -> dict[str, Any]:
    environment_key = bool(llm.api_key_env and os.getenv(llm.api_key_env))
    key_source = "environment" if environment_key else "config" if llm.api_key else "none"
    service_environment_token = bool(
        service.api_token_env and os.getenv(service.api_token_env)
    )
    return {
        "schema_version": "galatea.link.configuration.v1",
        "revision": int(revision),
        "server": {
            "profile": server.profile,
            "host": server.host,
            "port": server.port,
            "protocol_version": server.protocol_version,
            "auto_negotiate_version": server.auto_negotiate_version,
            "max_version_retries": server.max_version_retries,
            "game_id": server.game_id,
            "connect_timeout": server.connect_timeout,
            "trace_packets": server.trace_packets,
            "password_configured": bool(server.password),
        },
        "agent": {
            "name": agent.name,
            "deck": agent.deck,
            "prefer_second": agent.prefer_second,
        },
        "llm": {
            "enabled": llm.enabled,
            "provider": llm.provider,
            "base_url": llm.base_url,
            "api_key_env": llm.api_key_env,
            "api_key_configured": key_source != "none",
            "api_key_source": key_source,
            "model": llm.model,
            "timeout": llm.timeout,
            "max_tokens": llm.max_tokens,
            "temperature": llm.temperature,
            "response_format": llm.response_format,
            "trace_requests": llm.trace_requests,
            "thinking_mode": llm.thinking_mode,
            "cache_static_context": llm.cache_static_context,
            "cache_deck_text": llm.cache_deck_text,
            "compact_dynamic_observation": llm.compact_dynamic_observation,
        },
        "service": {
            "host": service.host,
            "port": service.port,
            "api_token_env": service.api_token_env,
            "api_token_configured": bool(service.api_token or service_environment_token),
            "event_queue_size": service.event_queue_size,
            "webui_enabled": service.webui_enabled,
            "allowed_origins": list(service.allowed_origins),
            "read_only": True,
        },
    }
