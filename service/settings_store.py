# Link 服务设置存储模块，持久化普通设置并将密钥隔离到本地文件

from __future__ import annotations

import json
import os
import tempfile
import stat
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping

from app_config import (
    AgentConfig,
    AppConfig,
    DecisionConfig,
    GameChatConfig,
    LlmConfig,
    ModelConfig,
    ServerConfig,
    load_app_config,
)
from core.runtime_controls import (
    apply_decision_control_patch,
    apply_game_chat_control_patch,
    build_controls_snapshot,
    validate_decision_controls,
    validate_game_chat_controls,
)
from service.configuration import (
    apply_configuration_patch,
    build_configuration_snapshot,
    validate_editable_configuration,
)


SETTINGS_SCHEMA_VERSION = "galatea.link.service_settings.v1"
MAX_SETTINGS_FILE_BYTES = 1024 * 1024
SECRET_SCHEMA_VERSION = "galatea.link.local_secrets.v1"
MAX_SECRET_FILE_BYTES = 64 * 1024


class LinkServiceSettingsStore:
    # 初始化配置基线并按需加载 Link 自有覆盖文件
    def __init__(
        self,
        config_path: str | Path,
        *,
        load_persisted: bool = True,
        persist_changes: bool = True,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.state_path = self.config_path.with_name("link_state.json")
        self.secret_path = self.config_path.with_name("link_secrets.json")
        self._base_config = load_app_config(self.config_path)
        self._server = self._base_config.server
        self._agent = self._base_config.agent
        self._llm = self._base_config.llm
        self._decision = self._base_config.decision
        self._game_chat = self._base_config.game_chat
        self._model = self._base_config.model
        self._controls_revision = 0
        self._model_revision = 0
        self._configuration_revision = 0
        self._local_llm_api_key: str | None = None
        self._persist_changes = persist_changes
        if load_persisted:
            self._load()
            self._load_secrets()

    # 返回合并离线覆盖后的完整应用配置
    def get_app_config(self) -> AppConfig:
        return replace(
            self._base_config,
            server=self._server,
            agent=self._agent,
            llm=self._llm,
            decision=self._decision,
            game_chat=self._game_chat,
            model=self._model,
        )

    # 返回脱敏后的服务器、Agent、LLM 和只读服务设置
    def get_configuration(self) -> dict[str, Any]:
        snapshot = build_configuration_snapshot(
            self._server,
            self._agent,
            self._llm,
            self._base_config.service,
            self._configuration_revision,
        )
        environment_key = bool(
            self._llm.api_key_env and os.getenv(self._llm.api_key_env)
        )
        if environment_key:
            key_source = "environment"
        elif self._local_llm_api_key:
            key_source = "local_file"
        elif self._base_config.llm.api_key:
            key_source = "config"
        else:
            key_source = "none"
        snapshot["llm"]["api_key_source"] = key_source
        snapshot["llm"]["api_key_configured"] = key_source != "none"
        snapshot["llm"]["local_api_key_configured"] = bool(
            self._local_llm_api_key
        )
        return snapshot

    # 保存或清除 Link 本机的 LLM API Key 且不写入普通状态文件
    def set_llm_api_key(self, api_key: str | None) -> dict[str, Any]:
        normalized = None if api_key is None else str(api_key).strip()
        if normalized is not None:
            if not normalized or "\x00" in normalized:
                raise ValueError("LLM API Key 不能为空或包含空字符")
            if len(normalized) > 16384:
                raise ValueError("LLM API Key 长度超过限制")
        self._local_llm_api_key = normalized
        self._llm = replace(
            self._llm,
            api_key=normalized or self._base_config.llm.api_key,
        )
        self._configuration_revision += 1
        self._persist_secrets()
        return self.get_configuration()

    # 预检非敏感配置补丁但暂不修改内存和磁盘
    def preview_configuration(
        self,
        patch: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> tuple[ServerConfig, AgentConfig, LlmConfig]:
        if not isinstance(patch, Mapping) or not patch:
            raise ValueError("配置补丁必须是非空映射")
        if (
            expected_revision is not None
            and expected_revision != self._configuration_revision
        ):
            raise RuntimeError(
                "配置版本冲突 "
                f"当前为 {self._configuration_revision} 请求为 {expected_revision}"
            )
        return apply_configuration_patch(
            self._server,
            self._agent,
            self._llm,
            patch,
        )

    # 提交已经通过预检的非敏感配置并递增版本
    def commit_configuration(
        self,
        server: ServerConfig,
        agent: AgentConfig,
        llm: LlmConfig,
    ) -> dict[str, Any]:
        validate_editable_configuration(server, agent, llm)
        self._server = server
        self._agent = agent
        self._llm = llm
        self._configuration_revision += 1
        self._persist()
        return self.get_configuration()

    # 返回离线和在线共用的宏观控制快照
    def get_controls(self) -> dict[str, Any]:
        return build_controls_snapshot(
            self._decision,
            self._game_chat,
            self._controls_revision,
        )

    # 预检宏观控制补丁但暂不修改内存和磁盘
    def preview_controls(
        self,
        patch: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> tuple[DecisionConfig, GameChatConfig]:
        if not isinstance(patch, Mapping) or not patch:
            raise ValueError("宏观控制补丁必须是非空映射")
        if (
            expected_revision is not None
            and expected_revision != self._controls_revision
        ):
            raise RuntimeError(
                "宏观控制版本冲突 "
                f"当前为 {self._controls_revision} 请求为 {expected_revision}"
            )
        decision = apply_decision_control_patch(self._decision, patch)
        game_chat = apply_game_chat_control_patch(self._game_chat, patch)
        return decision, game_chat

    # 提交已经通过预检的宏观控制配置并递增版本
    def commit_controls(
        self,
        decision: DecisionConfig,
        game_chat: GameChatConfig,
    ) -> dict[str, Any]:
        validate_decision_controls(decision)
        validate_game_chat_controls(game_chat)
        self._decision = decision
        self._game_chat = game_chat
        self._controls_revision += 1
        self._persist()
        return self.get_controls()

    # 返回当前模型选择及其独立版本号
    def get_model_selection(self) -> dict[str, Any]:
        return {
            "revision": self._model_revision,
            "weights_path": self._model.weights_path,
            "expected_model_id": self._model.expected_model_id,
            "protocol": self._model.protocol,
            "inference_backend": self._model.inference_backend,
            "assets_path": self._model.assets_path,
            "strict_asset_hashes": self._model.strict_asset_hashes,
        }

    # 保存经过仓库验证的模型与配套资产选择
    def select_model(
        self,
        record: Mapping[str, Any],
        *,
        assets_path: str | None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if expected_revision is not None and expected_revision != self._model_revision:
            raise RuntimeError(
                "模型选择版本冲突 "
                f"当前为 {self._model_revision} 请求为 {expected_revision}"
            )
        protocol_version = int(record["model_protocol_version"])
        if protocol_version != 3:
            raise ValueError("当前模型选择界面仅启用模型协议 V3")
        model_format = str(record["format"])
        backend = "onnxruntime" if model_format == "onnx" else "pytorch"
        self._model = replace(
            self._model,
            weights_path=f"./models/{record['primary']}",
            expected_model_id=str(record["model_id"]),
            protocol=f"v{protocol_version}",
            inference_backend=backend,
            assets_path=assets_path or self._model.assets_path,
        )
        self._model_revision += 1
        self._persist()
        return self.get_model_selection()

    # 从状态文件恢复不含敏感信息的策略和模型选择
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise ValueError("Link 状态文件必须是普通文件")
        if self.state_path.stat().st_size > MAX_SETTINGS_FILE_BYTES:
            raise ValueError("Link 状态文件超过大小限制")
        with self.state_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, Mapping):
            raise ValueError("Link 状态文件必须是 JSON 对象")
        if payload.get("schema_version") != SETTINGS_SCHEMA_VERSION:
            raise ValueError("Link 状态文件版本不受支持")

        raw_decision = payload.get("decision", {})
        raw_game_chat = payload.get("game_chat", {})
        raw_model = payload.get("model", {})
        raw_server = payload.get("server", {})
        raw_agent = payload.get("agent", {})
        raw_llm = payload.get("llm", {})
        if not all(
            isinstance(item, Mapping)
            for item in (
                raw_decision,
                raw_game_chat,
                raw_model,
                raw_server,
                raw_agent,
                raw_llm,
            )
        ):
            raise ValueError("Link 状态文件配置段必须是 JSON 对象")

        decision_fields = set(DecisionConfig.__dataclass_fields__)
        game_chat_fields = set(GameChatConfig.__dataclass_fields__)
        model_fields = {
            "weights_path",
            "expected_model_id",
            "protocol",
            "inference_backend",
            "assets_path",
            "strict_asset_hashes",
        }
        server_fields = {
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
        agent_fields = set(AgentConfig.__dataclass_fields__)
        llm_fields = set(LlmConfig.__dataclass_fields__) - {"api_key"}
        if set(raw_decision) - decision_fields:
            raise ValueError("Link 状态文件包含未知决策字段")
        if set(raw_game_chat) - game_chat_fields:
            raise ValueError("Link 状态文件包含未知聊天字段")
        if set(raw_model) - model_fields:
            raise ValueError("Link 状态文件包含未知模型字段")
        if set(raw_server) - server_fields:
            raise ValueError("Link 状态文件包含未知服务器字段")
        if set(raw_agent) - agent_fields:
            raise ValueError("Link 状态文件包含未知 Agent 字段")
        if set(raw_llm) - llm_fields:
            raise ValueError("Link 状态文件包含未知 LLM 字段")

        normalized_decision = dict(raw_decision)
        for field_name in (
            "force_llm_message_types",
            "autonomous_allowed_modes",
        ):
            if field_name in normalized_decision:
                normalized_decision[field_name] = tuple(
                    normalized_decision[field_name]
                )
        self._decision = replace(self._decision, **normalized_decision)
        self._game_chat = replace(self._game_chat, **dict(raw_game_chat))
        self._model = replace(self._model, **dict(raw_model))
        self._server = replace(self._server, **dict(raw_server))
        self._agent = replace(self._agent, **dict(raw_agent))
        self._llm = replace(self._llm, **dict(raw_llm))
        validate_decision_controls(self._decision)
        validate_game_chat_controls(self._game_chat)
        validate_editable_configuration(self._server, self._agent, self._llm)
        self._controls_revision = int(payload.get("controls_revision", 0))
        self._model_revision = int(payload.get("model_revision", 0))
        self._configuration_revision = int(
            payload.get("configuration_revision", 0)
        )
        if (
            self._controls_revision < 0
            or self._model_revision < 0
            or self._configuration_revision < 0
        ):
            raise ValueError("Link 状态文件版本号不能为负数")

    # 从本地密钥文件恢复 LLM API Key 且拒绝额外字段
    def _load_secrets(self) -> None:
        if not self.secret_path.exists():
            return
        if self.secret_path.is_symlink() or not self.secret_path.is_file():
            raise ValueError("Link 密钥文件必须是普通文件")
        if self.secret_path.stat().st_size > MAX_SECRET_FILE_BYTES:
            raise ValueError("Link 密钥文件超过大小限制")
        with self.secret_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, Mapping):
            raise ValueError("Link 密钥文件必须是 JSON 对象")
        if payload.get("schema_version") != SECRET_SCHEMA_VERSION:
            raise ValueError("Link 密钥文件版本不受支持")
        if set(payload) - {"schema_version", "llm_api_key"}:
            raise ValueError("Link 密钥文件包含未知字段")
        api_key = payload.get("llm_api_key")
        if not isinstance(api_key, str) or not api_key or "\x00" in api_key:
            raise ValueError("Link 密钥文件中的 LLM API Key 无效")
        if len(api_key) > 16384:
            raise ValueError("Link 密钥文件中的 LLM API Key 过长")
        self._local_llm_api_key = api_key
        self._llm = replace(self._llm, api_key=api_key)

    # 原子写入或清除只包含 LLM API Key 的本地密钥文件
    def _persist_secrets(self) -> None:
        if not self._persist_changes:
            return
        if self._local_llm_api_key is None:
            if self.secret_path.exists():
                self.secret_path.unlink()
            return
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix=f".{self.secret_path.name}.",
                suffix=".tmp",
                dir=self.secret_path.parent,
                encoding="utf-8",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(
                    {
                        "schema_version": SECRET_SCHEMA_VERSION,
                        "llm_api_key": self._local_llm_api_key,
                    },
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary_path, self.secret_path)
            temporary_path = None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    # 原子写入 Link 自有状态文件并避免持久化 API 密钥
    def _persist(self) -> None:
        if not self._persist_changes:
            return
        payload = {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "controls_revision": self._controls_revision,
            "model_revision": self._model_revision,
            "configuration_revision": self._configuration_revision,
            "server": {
                "profile": self._server.profile,
                "host": self._server.host,
                "port": self._server.port,
                "protocol_version": self._server.protocol_version,
                "auto_negotiate_version": self._server.auto_negotiate_version,
                "max_version_retries": self._server.max_version_retries,
                "game_id": self._server.game_id,
                "connect_timeout": self._server.connect_timeout,
                "trace_packets": self._server.trace_packets,
            },
            "agent": asdict(self._agent),
            "llm": {
                key: value
                for key, value in asdict(self._llm).items()
                if key != "api_key"
            },
            "decision": asdict(self._decision),
            "game_chat": asdict(self._game_chat),
            "model": {
                key: value
                for key, value in self.get_model_selection().items()
                if key != "revision"
            },
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix=f".{self.state_path.name}.",
                suffix=".tmp",
                dir=self.state_path.parent,
                encoding="utf-8",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.state_path)
            temporary_path = None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
