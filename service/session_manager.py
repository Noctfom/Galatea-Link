# Link 服务会话管理模块，隔离游戏运行实例与远程传输层

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core.link_events import EventStreamClosed, LinkEventBus, LinkEventSubscription
from service.model_repository import LinkModelRepository
from service.asset_manager import LinkAssetManager
from service.deck_repository import LinkDeckRepository
from service.settings_store import LinkServiceSettingsStore
from core.runtime_controls import apply_decision_control_patch
from service.configuration import build_configuration_snapshot, require_bool, require_float, require_int, validate_editable_configuration
from dataclasses import replace
from link_version import __version__


RuntimeBuilder = Callable[[Path], Any]
ASTRBOT_HEARTBEAT_LEASE_SECONDS = 60


class SessionNotRunningError(RuntimeError):
    pass


class LinkSessionManager:
    # 初始化只保存配置路径且不会提前加载模型
    def __init__(
        self,
        config_path: str | Path,
        *,
        runtime_builder: RuntimeBuilder | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        uses_default_builder = runtime_builder is None
        self._settings = LinkServiceSettingsStore(
            self.config_path,
            load_persisted=uses_default_builder,
            persist_changes=uses_default_builder,
        )
        self._models = LinkModelRepository(self.config_path.parent)
        self._assets = LinkAssetManager(
            self.config_path.parent,
            self._settings.get_model_selection,
        )
        self._decks = LinkDeckRepository(self.config_path.parent)
        self._runtime_builder = runtime_builder or self._build_runtime
        self._lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._asset_lock = asyncio.Lock()
        self._events = LinkEventBus()
        self._link: Any | None = None
        self._link_subscription: Any | None = None
        self._run_task: asyncio.Task | None = None
        self._relay_task: asyncio.Task | None = None
        self._generation = 0
        self._state = "stopped"
        self._last_error: str | None = None
        self._last_event_type: str | None = None
        self._session_config: Any | None = None
        self._session_deck_scope_id: str | None = None
        self._astrbot_clients: dict[str, dict[str, Any]] = {}

    # 返回当前是否持有仍在执行的游戏任务
    @property
    def is_running(self) -> bool:
        return self._state in {"starting", "running", "stopping"}

    # 创建远程事件订阅且不反向阻塞游戏线程
    def subscribe_events(self, max_queue_size: int = 256) -> LinkEventSubscription:
        return self._events.subscribe(max_queue_size=max_queue_size)

    # 返回不包含密码和密钥的服务会话状态
    def get_status(self) -> dict[str, Any]:
        runtime_status = None
        if self._link is not None:
            try:
                runtime_status = self._link.runtime.get_status()
            except Exception as error:
                self._last_error = str(error)
        return {
            "api_version": "galatea.link.api.v1",
            "link_version": __version__,
            "state": self._state,
            "running": self.is_running,
            "generation": self._generation,
            "last_event_type": self._last_event_type,
            "last_error": self._last_error,
            "configured_model": self._settings.get_model_selection(),
            "integrations": {
                "astrbot": self.get_astrbot_integration_status(),
            },
            "runtime": runtime_status,
        }

    # 记录 AstrBot 插件心跳并返回两端共同状态
    def record_astrbot_heartbeat(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        instance_name = str(payload.get("instance_name", "AstrBot")).strip()
        plugin_version = str(payload.get("plugin_version", "unknown")).strip()
        action = str(payload.get("action", "heartbeat")).strip().casefold()
        if not client_id or len(client_id) > 128 or "\x00" in client_id:
            raise ValueError("AstrBot client_id 无效")
        if not instance_name or len(instance_name) > 100 or "\x00" in instance_name:
            raise ValueError("AstrBot 实例名称无效")
        if len(plugin_version) > 64 or "\x00" in plugin_version:
            raise ValueError("AstrBot 插件版本无效")
        if action not in {"connect", "heartbeat", "disconnect"}:
            raise ValueError("AstrBot 心跳动作无效")
        now = time.time()
        previous = self._astrbot_clients.get(client_id)
        record = {
            "client_id": client_id,
            "instance_name": instance_name,
            "plugin_version": plugin_version,
            "remote_agent_enabled": bool(payload.get("remote_agent_enabled", True)),
            "agent_game_chat_enabled": bool(
                payload.get("agent_game_chat_enabled", True)
            ),
            "allow_agent_deck_edit": bool(payload.get("allow_agent_deck_edit", False)),
            "connected": action != "disconnect",
            "last_seen": now,
        }
        self._astrbot_clients[client_id] = record
        event_type = (
            "integration.astrbot.disconnected"
            if action == "disconnect"
            else "integration.astrbot.connected"
            if previous is None or not previous.get("connected")
            else "integration.astrbot.heartbeat"
        )
        if event_type != "integration.astrbot.heartbeat":
            self._events.publish(event_type, dict(record))
        return {
            "integration": dict(record),
            "link": {
                "state": self._state,
                "running": self.is_running,
                "generation": self._generation,
            },
            "lease_seconds": ASTRBOT_HEARTBEAT_LEASE_SECONDS,
        }

    # 汇总仍在心跳租约内的 AstrBot 插件连接
    def get_astrbot_integration_status(self) -> dict[str, Any]:
        now = time.time()
        clients = []
        for record in self._astrbot_clients.values():
            item = dict(record)
            item["connected"] = bool(
                item.get("connected")
                and now - item["last_seen"] <= ASTRBOT_HEARTBEAT_LEASE_SECONDS
            )
            item["age_seconds"] = round(max(0.0, now - item["last_seen"]), 1)
            clients.append(item)
        clients.sort(key=lambda item: item["last_seen"], reverse=True)
        return {
            "connected": any(item["connected"] for item in clients),
            "active_count": sum(1 for item in clients if item["connected"]),
            "clients": clients[:8],
        }

    # 按需构建 Link 并启动单个游戏会话
    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self.is_running:
                return self.get_status()
            await self._discard_finished_session()
            self._state = "starting"
            self._last_error = None
            self._events.publish("service.session.starting")
            try:
                link = await asyncio.to_thread(
                    self._runtime_builder,
                    self.config_path,
                )
                subscription = link.runtime.subscribe_events()
            except Exception as error:
                self._state = "failed"
                self._last_error = str(error)
                self._events.publish(
                    "service.session.failed",
                    {"error": str(error)},
                )
                raise

            self._generation += 1
            generation = self._generation
            self._link = link
            self._link_subscription = subscription
            self._state = "running"
            self._relay_task = asyncio.create_task(
                self._relay_events(subscription, generation),
                name=f"linkd-events-{generation}",
            )
            self._run_task = asyncio.create_task(
                self._run_link(link, generation),
                name=f"linkd-session-{generation}",
            )
            self._events.publish(
                "service.session.started",
                {"generation": generation},
            )
            return self.get_status()

    # 停止当前游戏会话并保留服务端和 WebSocket 连接
    async def stop(self) -> bool:
        async with self._lock:
            if self._link is None:
                await self._discard_finished_session()
                self._state = "stopped"
                self._session_config = None
                self._session_deck_scope_id = None
                return False
            generation = self._generation
            link = self._link
            subscription = self._link_subscription
            run_task = self._run_task
            relay_task = self._relay_task
            self._state = "stopping"

        try:
            await link.close()
        finally:
            if subscription is not None:
                subscription.close()
            await self._finish_task(run_task)
            await self._finish_task(relay_task)
            async with self._lock:
                if generation == self._generation:
                    self._clear_session()
                    self._session_config = None
                    self._session_deck_scope_id = None
                    self._events.publish(
                        "service.session.stopped",
                        {"generation": generation},
                    )
        return True

    # 读取当前运行时宏观控制快照
    def get_controls(self) -> dict[str, Any]:
        controls = self._settings.get_controls()
        if self._link is None:
            return controls
        try:
            runtime_controls = self._link.runtime.get_controls()
        except Exception:
            return controls
        controls["intervention"] = runtime_controls.get(
            "intervention",
            controls["intervention"],
        )
        controls["autonomy"]["active_override"] = runtime_controls.get(
            "autonomy",
            {},
        ).get("active_override")
        return controls

    # 原子更新介入策略、自主控制和聊天设置
    async def update_controls(
        self,
        patch: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        async with self._control_lock:
            decision, game_chat = self._settings.preview_controls(
                patch,
                expected_revision=expected_revision,
            )
            if self._link is not None:
                await self._link.runtime.update_controls(
                    patch,
                    source="link.service",
                )
            controls = self._settings.commit_controls(decision, game_chat)
            self._events.publish(
                "service.controls.updated",
                {"controls": controls, "session_running": self.is_running},
            )
            return self.get_controls()

    # 返回脱敏配置、可用卡组和应用时机
    def get_configuration(self) -> dict[str, Any]:
        configuration = self._settings.get_configuration()
        configuration["available_decks"] = self._list_decks()
        configuration["deck_catalog"] = self._decks.discover()
        configuration["session_running"] = self.is_running
        configuration["applies_after_restart"] = self.is_running
        return configuration

    # 返回当前仅存于内存的远程决斗会话配置
    def get_session_configuration(self) -> dict[str, Any]:
        config = self._session_config or self._settings.get_app_config()
        snapshot = build_configuration_snapshot(
            config.server,
            config.agent,
            config.llm,
            config.service,
            0,
        )
        snapshot["session_configured"] = self._session_config is not None
        snapshot["session_running"] = self.is_running
        snapshot["decision"] = {
            "mode": config.decision.mode,
            "agent_backend": config.decision.agent_backend,
            "core_policy_mode": config.decision.core_policy_mode,
            "core_temperature": config.decision.core_temperature,
            "core_confidence_threshold": config.decision.core_confidence_threshold,
            "llm_time_budget": config.decision.llm_time_budget,
        }
        return snapshot

    # 保存一次远程决斗使用的连接和 Agent 配置且不写入磁盘
    async def configure_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.is_running:
            raise RuntimeError("对局运行中不能修改会话连接配置")
        if not isinstance(payload, Mapping):
            raise ValueError("会话配置必须是 JSON 对象")
        if set(payload) - {"server", "agent", "decision", "deck_scope_id"}:
            raise ValueError("会话配置包含未知分区")
        base = self._settings.get_app_config()
        server_patch = payload.get("server", {})
        agent_patch = payload.get("agent", {})
        decision_patch = payload.get("decision", {})
        if not all(isinstance(item, Mapping) for item in (server_patch, agent_patch, decision_patch)):
            raise ValueError("会话配置分区必须是 JSON 对象")
        allowed_server = {"host", "port", "password", "protocol_version", "auto_negotiate_version", "max_version_retries", "game_id", "connect_timeout", "trace_packets"}
        allowed_agent = {"name", "deck", "prefer_second"}
        allowed_decision = {"mode", "agent_backend", "core_policy_mode", "core_temperature", "core_confidence_threshold", "force_llm_message_types", "include_core_suggestion", "llm_time_budget"}
        if set(server_patch) - allowed_server or set(agent_patch) - allowed_agent or set(decision_patch) - allowed_decision:
            raise ValueError("会话配置包含未知字段")
        server_values = {}
        if "host" in server_patch:
            server_values["host"] = str(server_patch["host"]).strip()
        for field_name in ("port", "protocol_version", "max_version_retries", "game_id"):
            if field_name in server_patch:
                server_values[field_name] = require_int(server_patch[field_name], f"server.{field_name}")
        if "connect_timeout" in server_patch:
            server_values["connect_timeout"] = require_float(server_patch["connect_timeout"], "server.connect_timeout")
        if "trace_packets" in server_patch:
            server_values["trace_packets"] = require_bool(server_patch["trace_packets"], "server.trace_packets")
        if "auto_negotiate_version" in server_patch:
            server_values["auto_negotiate_version"] = require_bool(server_patch["auto_negotiate_version"], "server.auto_negotiate_version")
        if "password" in server_patch:
            password = str(server_patch["password"])
            if len(password) > 64 or "\x00" in password:
                raise ValueError("游戏服务器密码长度无效")
            server_values["password"] = password
        agent_values = {key: str(value).strip() for key, value in agent_patch.items() if key in {"name", "deck"}}
        if "prefer_second" in agent_patch:
            agent_values["prefer_second"] = require_bool(agent_patch["prefer_second"], "agent.prefer_second")
        agent = replace(base.agent, **agent_values)
        deck_scope_id = payload.get("deck_scope_id")
        if deck_scope_id is not None:
            deck_scope_id = str(deck_scope_id).strip()
        self._ensure_deck_available(agent.deck, deck_scope_id)
        server = replace(base.server, **server_values)
        validate_editable_configuration(server, agent, base.llm)
        decision = apply_decision_control_patch(base.decision, {"intervention": dict(decision_patch)}) if decision_patch else base.decision
        self._session_config = replace(base, server=server, agent=agent, decision=decision)
        self._session_deck_scope_id = deck_scope_id
        return self.get_session_configuration()

    # 保存下一次 Link 会话使用的服务器、Agent 和 LLM 设置
    async def update_configuration(
        self,
        patch: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        async with self._control_lock:
            server, agent, llm = self._settings.preview_configuration(
                patch,
                expected_revision=expected_revision,
            )
            self._ensure_deck_available(agent.deck)
            configuration = self._settings.commit_configuration(
                server,
                agent,
                llm,
            )
            self._events.publish(
                "service.configuration.updated",
                {
                    "configuration": configuration,
                    "applies_after_restart": self.is_running,
                },
            )
            return self.get_configuration()

    # 保存或清除 Link 本机 LLM API Key 并返回脱敏状态
    async def update_llm_api_key(
        self,
        api_key: str | None,
    ) -> dict[str, Any]:
        async with self._control_lock:
            configuration = self._settings.set_llm_api_key(api_key)
            self._events.publish(
                "service.llm_secret.updated",
                {
                    "configured": configuration["llm"]["api_key_configured"],
                    "source": configuration["llm"]["api_key_source"],
                    "applies_after_restart": self.is_running,
                },
            )
            return self.get_configuration()

    # 使用表单草稿执行一次不发送游戏协议数据的 TCP 连通测试
    async def test_server_configuration(
        self,
        patch: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = self._session_config or self._settings.get_app_config()
        server = config.server
        if patch:
            server, _, _ = self._settings.preview_configuration(patch)
        started_at = time.perf_counter()
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(server.host, server.port),
                timeout=server.connect_timeout,
            )
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
        return {
            "schema_version": "galatea.link.server_test.v1",
            "ok": True,
            "host": server.host,
            "port": server.port,
            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            "scope": "tcp_only",
        }

    # 使用表单草稿执行一次最小 LLM 结构化输出测试
    async def test_llm_configuration(
        self,
        patch: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        from agents.llm_smoke import run_llm_smoke_test

        config = self._settings.get_app_config()
        llm = config.llm
        if patch:
            _, _, llm = self._settings.preview_configuration(patch)
        return await run_llm_smoke_test(llm)

    # 返回已安装模型、无效制品和当前选择
    def get_model_catalog(self) -> dict[str, Any]:
        selection = self._settings.get_model_selection()
        catalog = self._models.discover(selection["weights_path"])
        catalog["selection"] = selection
        catalog["session_running"] = self.is_running
        catalog["selection_applies_after_restart"] = self.is_running
        catalog["packages"] = self._models.list_packages()
        return catalog

    # 返回 Link 本地卡组和当前 AstrBot 会话可见的导入卡组
    def get_deck_catalog(self, scope_id: str | None = None) -> dict[str, Any]:
        decks = self._decks.discover(scope_id=scope_id)
        return {
            "schema_version": "galatea.link.deck_catalog.v1",
            "scope": "current_astrbot_session" if scope_id else "link_local",
            "decks": decks,
        }

    # 导入或显式覆盖 Link 根目录中的本地 YDK 卡组
    async def import_local_deck(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("本地卡组导入参数必须是对象")
        unknown = set(payload) - {"filename", "ydk_text", "overwrite"}
        if unknown:
            raise ValueError(f"本地卡组导入包含未知字段: {sorted(unknown)}")
        overwrite = payload.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite 必须是布尔值")
        async with self._control_lock:
            record = await asyncio.to_thread(
                self._decks.import_local_ydk,
                payload.get("filename"),
                payload.get("ydk_text"),
                overwrite=overwrite,
            )
            self._events.publish(
                "service.deck.local_imported",
                {
                    "deck_ref": record["deck_ref"],
                    "display_name": record["display_name"],
                    "overwritten": overwrite,
                },
            )
            return record

    # 删除未被当前配置或临时会话选中的 Link 本地卡组
    async def delete_local_deck(self, deck_ref: str) -> dict[str, Any]:
        normalized_ref = str(deck_ref or "").strip()
        if not normalized_ref:
            raise ValueError("Link 本地卡组引用不能为空")
        async with self._control_lock:
            configured_ref = str(self._settings.get_app_config().agent.deck)
            session_ref = (
                str(self._session_config.agent.deck)
                if self._session_config is not None
                else ""
            )
            if normalized_ref in {configured_ref, session_ref}:
                raise RuntimeError("当前配置正在使用该卡组，请先选择其他卡组再删除")
            record = await asyncio.to_thread(
                self._decks.delete_local_deck,
                normalized_ref,
            )
            self._events.publish(
                "service.deck.local_deleted",
                {
                    "deck_ref": record["deck_ref"],
                    "display_name": record["display_name"],
                },
            )
            return record

    # 在停止状态下按单卡操作修改 Link 本地卡组
    async def edit_local_deck(
        self,
        deck_ref: str,
        operations: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_ref = str(deck_ref or "").strip()
        if not normalized_ref:
            raise ValueError("Link 本地卡组引用不能为空")
        if not isinstance(operations, list):
            raise ValueError("卡组修改 operations 必须是数组")
        async with self._control_lock:
            if self.is_running:
                raise RuntimeError("对局运行中不能修改 Link 本地卡组")
            record = await asyncio.to_thread(
                self._decks.edit_local_deck,
                normalized_ref,
                operations,
            )
            self._events.publish(
                "service.deck.local_edited",
                {
                    "deck_ref": record["deck_ref"],
                    "display_name": record["display_name"],
                    "counts": record["counts"],
                },
            )
            return record

    # 导入当前 AstrBot 会话的工具箱 YDK 副本
    async def import_astrbot_deck(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = await asyncio.to_thread(self._decks.import_astrbot_ydk, payload)
        self._events.publish(
            "service.deck.imported",
            {
                "deck_ref": record["deck_ref"],
                "display_name": record["display_name"],
                "source": record["source"],
            },
        )
        return record

    # 将 Link 本地卡组物化为当前 AstrBot 会话的临时可编辑副本
    async def copy_local_deck(
        self,
        scope_id: str,
        deck_ref: str,
        instance_name: str,
    ) -> dict[str, Any]:
        record = await asyncio.to_thread(
            self._decks.copy_local_to_astrbot,
            scope_id,
            deck_ref,
            instance_name,
        )
        self._events.publish(
            "service.deck.local_copied",
            {
                "deck_ref": record["deck_ref"],
                "display_name": record["display_name"],
                "source": record["source"],
            },
        )
        return record

    # 修改当前远程会话已经选中的 AstrBot 临时对战卡组
    async def edit_current_session_deck(
        self,
        scope_id: str,
        operations: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_scope = str(scope_id).strip()
        if not normalized_scope:
            raise ValueError("AstrBot 卡组作用域不能为空")
        async with self._lock:
            if self.is_running:
                raise RuntimeError("对局运行中不能修改当前对战卡组")
            if self._session_config is None:
                raise RuntimeError("请先配置本次 Link 对局并选择临时卡组")
            if self._session_deck_scope_id != normalized_scope:
                raise PermissionError("只能修改当前 AstrBot 会话选择的临时卡组")
            deck_ref = str(self._session_config.agent.deck)
            if not deck_ref.startswith("astrbot:"):
                raise PermissionError("当前选择不是可编辑的 AstrBot 临时卡组")
            record = await asyncio.to_thread(
                self._decks.edit_astrbot_deck,
                normalized_scope,
                deck_ref,
                operations,
            )
            self._events.publish(
                "service.deck.edited",
                {
                    "deck_ref": record["deck_ref"],
                    "display_name": record["display_name"],
                    "source": record["source"],
                },
            )
            return record

    # 列出 Link 根卡组目录中的可选择 YDK 名称
    def _list_decks(self) -> list[str]:
        return [item["deck_ref"] for item in self._decks.discover()]

    # 拒绝保存当前 Link 无法从根卡组目录加载的卡组
    def _ensure_deck_available(
        self,
        deck_name: str,
        scope_id: str | None = None,
    ) -> None:
        if not self._decks.contains(deck_name, scope_id=scope_id):
            raise ValueError(f"找不到可用卡组: {deck_name}")

    # 选择下次 Link 会话加载的模型和配套语义资产
    async def select_model(
        self,
        filename: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        async with self._control_lock:
            record = await asyncio.to_thread(self._models.get_model, filename)
            if not record.get("assets_available"):
                raise RuntimeError("所选模型缺少可用的模型协议 V3 语义资产")
            selection = self._settings.select_model(
                record,
                assets_path=record.get("assets_path"),
                expected_revision=expected_revision,
            )
            self._events.publish(
                "service.model.selected",
                {
                    "selection": selection,
                    "applies_after_restart": self.is_running,
                },
            )
            return self.get_model_catalog()

    # 安全导入 GKG 包并返回刷新后的模型仓库
    async def import_model_package(
        self,
        package_path: str | Path,
        package_name: str,
        *,
        activate: bool = True,
    ) -> dict[str, Any]:
        async with self._control_lock:
            current_selection = self._settings.get_model_selection()
            imported = await asyncio.to_thread(
                self._models.import_gkg,
                package_path,
                package_name,
                current_selection.get("expected_model_id") or None,
            )
            selected = None
            if activate and imported["models"]:
                preferred = sorted(
                    imported["models"],
                    key=lambda item: (
                        item.get("format") != "onnx",
                        -int(item.get("iteration", 0)),
                    ),
                )[0]
                record = self._models.get_model(preferred["primary"])
                selected = self._settings.select_model(
                    record,
                    assets_path=imported.get("assets_path") or record.get("assets_path"),
                )
            elif activate and imported.get("assets_path"):
                current_filename = Path(str(current_selection["weights_path"])).name
                try:
                    record = self._models.get_model(current_filename)
                except (FileNotFoundError, ValueError):
                    record = None
                if (
                    record is not None
                    and record.get("model_id") == imported.get("asset_target_model_id")
                ):
                    selected = self._settings.select_model(
                        record,
                        assets_path=imported["assets_path"],
                    )
            self._events.publish(
                "service.model.imported",
                {
                    "package_name": package_name,
                    "model_id": imported["model_id"],
                    "selected": selected,
                    "applies_after_restart": self.is_running,
                },
            )
            return {
                "imported": imported,
                "selection": selected,
                "catalog": self.get_model_catalog(),
            }

    # 导入专用部署目录中已经存在的 GKG 包
    async def import_local_model_package(
        self,
        filename: str,
        *,
        activate: bool = True,
    ) -> dict[str, Any]:
        path = self._models.resolve_package(filename)
        return await self.import_model_package(
            path,
            filename,
            activate=activate,
        )

    # 返回当前模型路径对应的卡库语义脚本和 142 兜底池状态
    async def get_asset_status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._assets.get_status)

    # 在停止状态同步 CDB 和官方 Lua 脚本并刷新进程内卡库
    async def sync_card_data(
        self,
        *,
        cdb_url: str,
        script_repository: str,
        force_scripts: bool,
    ) -> dict[str, Any]:
        if self.is_running:
            raise RuntimeError("请先停止 Link 会话再更新卡库和脚本")
        async with self._asset_lock:
            from utils.card_reader import card_db

            card_db.close()
            try:
                result = await asyncio.to_thread(
                    self._assets.sync_card_data,
                    cdb_url=cdb_url,
                    script_repository=script_repository,
                    force_scripts=force_scripts,
                )
            finally:
                asset_root = self._assets.get_active_asset_root()
                selected = asset_root / "cards.cdb"
                card_db.reload(
                    selected if selected.is_file() else self.config_path.parent / "cards.cdb"
                )
            self._events.publish("service.assets.card_data_updated", result)
            return result

    # 在停止状态同步并校验远程模型协议 V3 语义资产
    async def sync_semantic_assets(self, remote_url: str) -> dict[str, Any]:
        if self.is_running:
            raise RuntimeError("请先停止 Link 会话再更新语义资产")
        async with self._asset_lock:
            result = await asyncio.to_thread(
                self._assets.sync_semantic_bundle,
                remote_url,
            )
            self._events.publish("service.assets.semantics_updated", result)
            return result

    # 在停止状态按模型协议 V3 解析 Lua 并重建语义向量
    async def rebuild_semantic_assets(
        self,
        *,
        remote_url: str | None,
        clear_existing: bool,
    ) -> dict[str, Any]:
        if self.is_running:
            raise RuntimeError("请先停止 Link 会话再执行本机语义化")
        async with self._asset_lock:
            result = await asyncio.to_thread(
                self._assets.rebuild_semantic_bundle,
                remote_url=remote_url,
                clear_existing=clear_existing,
            )
            self._events.publish("service.assets.semantics_rebuilt", result)
            return result

    # 更新当前模型资产目录中的 142 宣言兜底池
    async def update_meta_staples(
        self,
        operation: str,
        card_codes: list[Any],
    ) -> dict[str, Any]:
        async with self._asset_lock:
            staples = await asyncio.to_thread(
                self._assets.update_meta_staples,
                operation,
                card_codes,
            )
            result = {
                "meta_staples": staples,
                "applies_after_restart": self.is_running,
            }
            self._events.publish("service.assets.meta_staples_updated", result)
            return result

    # 返回浏览器上传 GKG 使用的受限暂存目录
    def get_model_package_directory(self) -> Path:
        return self._models.package_root

    # 读取最近一次提供给 LLM 的可见信息快照
    def get_latest_observation(self) -> dict[str, Any] | None:
        return self._require_runtime().get_latest_observation()

    # 返回当前远程智能体尚未提交的决策请求
    def get_pending_decision(self) -> dict[str, Any] | None:
        return self._require_runtime().get_pending_decision()

    # 提交远程智能体的合法动作并唤醒 Link 决策任务
    async def submit_remote_decision(
        self,
        request_id: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._require_runtime().submit_remote_decision(
            request_id,
            payload,
        )

    # 将远程文本发送到游戏内聊天接口
    async def send_game_chat(self, text: str) -> dict[str, Any]:
        return await self._require_runtime().send_game_chat(
            text,
            source="link.service",
        )

    # 将外部社交消息异步投递到当前 Link 事件流
    async def publish_external_message(
        self,
        text: str,
        *,
        source: str,
        sender_id: str | None = None,
    ) -> dict[str, Any]:
        event = await self._require_runtime().publish_external_message(
            text,
            source=source,
            sender_id=sender_id,
        )
        return event.to_dict() if event is not None else {
            "event_type": "external.message.received",
            "payload": {"text": str(text).strip(), "source": source, "sender_id": sender_id},
        }

    # 读取受限长度的游戏聊天历史
    def get_game_chat_history(self, max_messages: int = 12) -> list[dict[str, Any]]:
        return self._require_runtime().get_game_chat_history(
            max_messages=max_messages
        )

    # 关闭游戏会话和服务级事件总线
    async def close(self) -> None:
        try:
            await self.stop()
        finally:
            self._events.close()

    # 从配置文件按需创建 Link 实例
    def _build_runtime(self, config_path: Path) -> Any:
        from galatea_link import create_link_from_config

        config = self._session_config or self._settings.get_app_config()
        return create_link_from_config(config)

    # 运行游戏连接并记录自然断开或后台异常
    async def _run_link(self, link: Any, generation: int) -> None:
        try:
            await link.start()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._last_error = str(error)
            self._events.publish(
                "service.session.failed",
                {"generation": generation, "error": str(error)},
            )
        finally:
            if generation == self._generation and self._state != "stopping":
                self._state = "stopped"
                self._events.publish(
                    "service.session.ended",
                    {"generation": generation},
                )

    # 将 Link 内部事件转发为跨会话稳定事件流
    async def _relay_events(self, subscription: Any, generation: int) -> None:
        try:
            while generation == self._generation:
                event = await subscription.get()
                self._last_event_type = str(event.event_type)
                self._events.publish(event.event_type, event.payload)
        except asyncio.CancelledError:
            raise
        except EventStreamClosed:
            return
        finally:
            subscription.close()

    # 返回活动运行时并拒绝对已停止会话的操作
    def _require_runtime(self) -> Any:
        if self._link is None or not self.is_running:
            raise SessionNotRunningError("Galatea Link 当前没有运行中的对局会话")
        return self._link.runtime

    # 等待后台任务结束并在超时后安全取消
    async def _finish_task(self, task: asyncio.Task | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    # 清理已经结束的旧会话资源
    async def _discard_finished_session(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            return
        if self._link_subscription is not None:
            self._link_subscription.close()
        await self._finish_task(self._relay_task)
        if self._link is not None:
            try:
                await self._link.close()
            except Exception:
                pass
        self._clear_session()

    # 清空单个游戏会话引用但保留诊断信息
    def _clear_session(self) -> None:
        self._link = None
        self._link_subscription = None
        self._run_task = None
        self._relay_task = None
        self._state = "stopped"
