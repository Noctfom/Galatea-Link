# Link 独立服务测试，验证懒加载、鉴权和远程控制契约

import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from app_config import ServiceConfig
from core.link_events import LinkEventBus
from service.http_api import create_http_app, validate_service_security
from service.deck_repository import LinkDeckRepository
from service.session_manager import LinkSessionManager


YDK_TEXT = """#main
89631139
89631139
#extra
23995346
!side
46986414
"""


class FakeRuntime:
    # 初始化测试运行时和可观察状态
    def __init__(self, event_bus: LinkEventBus) -> None:
        self._events = event_bus
        self._revision = 0
        self._controls = {
            "schema_version": "galatea.runtime_controls.v1",
            "revision": 0,
            "intervention": {"mode": "core_only"},
            "baseline_intervention": {
                "mode": "core_only",
                "core_confidence_threshold": 0.65,
            },
            "autonomy": {"enabled": False},
            "game_chat": {"enabled": True},
        }
        self._history = []

    # 创建测试事件订阅
    def subscribe_events(self, max_queue_size: int = 128):
        return self._events.subscribe(max_queue_size=max_queue_size)

    # 返回测试运行状态
    def get_status(self):
        return {"connected": True, "duel_active": False}

    # 返回测试宏观控制
    def get_controls(self):
        return dict(self._controls)

    # 应用测试控制补丁并模拟 revision 更新
    async def update_controls(self, patch, *, source, expected_revision=None):
        if expected_revision is not None and expected_revision != self._revision:
            raise RuntimeError("宏观控制版本冲突")
        self._revision += 1
        self._controls["revision"] = self._revision
        if "intervention" in patch:
            self._controls["baseline_intervention"].update(patch["intervention"])
        return dict(self._controls)

    # 返回测试观察快照
    def get_latest_observation(self):
        return {"schema_version": "galatea.llm_observation.v1"}

    # 记录测试聊天消息
    async def send_game_chat(self, text, *, source):
        item = {"text": text, "source": source}
        self._history.append(item)
        return item

    # 返回最近测试聊天消息
    def get_game_chat_history(self, max_messages=12):
        return self._history[-max_messages:]

    # 返回测试用的远程决策请求
    def get_pending_decision(self):
        return None

    # 接收测试用的远程决策提交
    def submit_remote_decision(self, request_id, payload):
        return {"accepted": True, "request_id": request_id}

    # 记录并发布外部社交消息测试事件
    async def publish_external_message(self, text, *, source, sender_id=None):
        return self._events.publish(
            "external.message.received",
            {
                "text": text,
                "source": source,
                "sender_id": sender_id,
            },
        )


class FakeLink:
    # 初始化不会访问模型和网络的测试 Link
    def __init__(self) -> None:
        self.events = LinkEventBus()
        self.runtime = FakeRuntime(self.events)
        self._closed = asyncio.Event()

    # 模拟持续运行的游戏连接
    async def start(self) -> None:
        self.events.publish("connection.connected")
        await self._closed.wait()

    # 模拟关闭游戏连接并结束事件流
    async def close(self) -> None:
        if self._closed.is_set():
            return
        self.events.publish("link.closed")
        self.events.close()
        self._closed.set()


class LinkSessionManagerTests(unittest.IsolatedAsyncioTestCase):
    # 验证服务初始化不会提前创建 Link 或加载模型
    async def test_runtime_is_built_only_when_session_starts(self):
        created = []

        # 记录懒加载构建次数
        def build_runtime(config_path: Path):
            created.append(config_path)
            return FakeLink()

        manager = LinkSessionManager("config.yaml", runtime_builder=build_runtime)
        self.assertEqual(created, [])
        status = await manager.start()

        self.assertEqual(len(created), 1)
        self.assertTrue(status["running"])
        self.assertEqual(status["state"], "running")
        self.assertTrue(await manager.stop())
        self.assertFalse(manager.get_status()["running"])
        await manager.close()

    # 验证服务级事件流可跨游戏运行时向外转发
    async def test_relays_runtime_events(self):
        link = FakeLink()
        manager = LinkSessionManager("config.yaml", runtime_builder=lambda _: link)
        remote_events = manager.subscribe_events()

        await manager.start()
        received_types = []
        while "connection.connected" not in received_types:
            received_types.append((await remote_events.get()).event_type)

        self.assertIn("service.session.started", received_types)
        self.assertIn("connection.connected", received_types)
        remote_events.close()
        await manager.close()

    # 验证远程会话配置包含密码时只传给启动实例而不写入状态文件
    async def test_ephemeral_session_configuration(self):
        captured = []
        manager = None

        def build_runtime(config_path: Path):
            captured.append(manager._session_config)
            return FakeLink()

        manager = LinkSessionManager("config.yaml", runtime_builder=build_runtime)
        configured = await manager.configure_session(
            {
                "server": {"host": "127.0.0.1", "port": 7911, "password": "ephemeral-secret"},
                "agent": {"deck": "神秘白龙"},
                "decision": {"agent_backend": "remote_astrbot", "mode": "llm_only"},
            }
        )
        self.assertTrue(configured["session_configured"])
        self.assertNotIn("password", configured["server"])
        self.assertEqual(configured["decision"]["agent_backend"], "remote_astrbot")
        await manager.start()
        self.assertEqual(captured[0].server.password, "ephemeral-secret")
        self.assertEqual(captured[0].decision.agent_backend, "remote_astrbot")
        await manager.close()


class LinkHttpApiTests(unittest.IsolatedAsyncioTestCase):
    # 创建带令牌的隔离 HTTP 测试客户端
    async def asyncSetUp(self):
        self.created = []

        # 为 HTTP 测试创建独立假 Link
        def build_runtime(config_path: Path):
            link = FakeLink()
            self.created.append(link)
            return link

        self.manager = LinkSessionManager(
            "config.yaml",
            runtime_builder=build_runtime,
        )
        config = ServiceConfig(api_token="test-token")
        app = create_http_app(self.manager, config)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    # 清理测试服务和全部后台任务
    async def asyncTearDown(self):
        await self.client.close()

    # 验证公开健康检查不会加载模型且私有接口需要令牌
    async def test_health_is_public_and_status_is_authenticated(self):
        health = await self.client.get("/api/v1/health")
        self.assertEqual(health.status, 200)
        self.assertEqual(self.created, [])
        self.assertEqual((await health.json())["data"]["version"], "3.0.0")

        unauthorized = await self.client.get("/api/v1/status")
        self.assertEqual(unauthorized.status, 401)

        authorized = await self.client.get(
            "/api/v1/status",
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertEqual(authorized.status, 200)

    # 验证控制台页面和零构建静态资源可以直接由 Link 服务提供
    async def test_webui_static_assets_are_served(self):
        page = await self.client.get("/")
        self.assertEqual(page.status, 200)
        page_text = await page.text()
        self.assertIn("Galatea Link", page_text)
        self.assertIn("data-theme=\"light\"", page_text)
        self.assertIn("决策策略", page_text)
        self.assertIn("配置中心", page_text)
        self.assertIn("helpDialog", page_text)
        self.assertIn("模型仓库", page_text)
        self.assertIn("卡组仓库", page_text)
        self.assertIn("localDeckImportForm", page_text)
        self.assertIn("deckEditorDialog", page_text)
        self.assertIn("运行资产", page_text)
        self.assertIn("metaStaplesCodesInput", page_text)
        self.assertIn("rebuildSemanticsButton", page_text)
        self.assertIn("llmApiKeyInput", page_text)
        self.assertIn("agentBackendInput", page_text)
        self.assertIn("astrbotMetric", page_text)
        self.assertIn("/brand-logo.png", page_text)

        logo = await self.client.get("/brand-logo.png")
        self.assertEqual(logo.status, 200)
        self.assertEqual(logo.headers["Content-Type"], "image/png")
        self.assertGreater(len(await logo.read()), 1000)

        stylesheet = await self.client.get("/assets/app.css")
        self.assertEqual(stylesheet.status, 200)
        self.assertIn("text/css", stylesheet.headers["Content-Type"])
        self.assertIn("--accent", await stylesheet.text())

        script = await self.client.get("/assets/app.js")
        self.assertEqual(script.status, 200)
        self.assertIn("javascript", script.headers["Content-Type"])
        script_text = await script.text()
        self.assertIn("initializeConsole", script_text)
        self.assertIn("applyLocalDeckEdit", script_text)

    # 验证本地卡组 HTTP 接口支持导入单卡修改和删除
    async def test_local_deck_management_api(self):
        headers = {"Authorization": "Bearer test-token"}
        with tempfile.TemporaryDirectory() as directory:
            self.manager._decks = LinkDeckRepository(directory)
            imported = await self.client.post(
                "/api/v1/decks/local",
                headers=headers,
                json={"filename": "网页卡组.ydk", "ydk_text": YDK_TEXT},
            )
            self.assertEqual(imported.status, 201)
            self.assertEqual((await imported.json())["data"]["deck_ref"], "网页卡组")

            duplicate = await self.client.post(
                "/api/v1/decks/local",
                headers=headers,
                json={"filename": "网页卡组.ydk", "ydk_text": YDK_TEXT},
            )
            self.assertEqual(duplicate.status, 409)

            edited = await self.client.patch(
                "/api/v1/decks/local",
                headers=headers,
                json={
                    "deck_ref": "网页卡组",
                    "operations": [
                        {"operation": "add", "code": 46986414, "section": "main"}
                    ],
                },
            )
            self.assertEqual(edited.status, 200)
            self.assertEqual((await edited.json())["data"]["counts"]["main"], 3)

            deleted = await self.client.delete(
                "/api/v1/decks/local",
                headers=headers,
                json={"deck_ref": "网页卡组"},
            )
            self.assertEqual(deleted.status, 200)
            self.assertFalse((Path(directory) / "decks/网页卡组.ydk").exists())

    # 验证停止状态可以读取模型协议 V3 配套运行资产
    async def test_asset_catalog_is_available_while_stopped(self):
        response = await self.client.get(
            "/api/v1/assets",
            headers={"Authorization": "Bearer test-token"},
        )

        self.assertEqual(response.status, 200)
        payload = (await response.json())["data"]
        self.assertEqual(payload["schema_version"], "galatea.link.assets.v1")
        self.assertIn("card_database", payload)
        self.assertIn("semantic_complete", payload)
        self.assertIn("meta_staples", payload)

    # 验证本机语义化接口在执行前拒绝无效布尔参数
    async def test_semantic_rebuild_rejects_invalid_mode_payload(self):
        response = await self.client.post(
            "/api/v1/assets/semantics/rebuild",
            headers={"Authorization": "Bearer test-token"},
            json={"clear_existing": "false"},
        )

        self.assertEqual(response.status, 400)
        payload = (await response.json())["error"]
        self.assertEqual(payload["code"], "invalid_clear_existing")

    # 验证停止状态也能读取和保存下次会话使用的统一策略
    async def test_controls_are_editable_before_session_start(self):
        headers = {"Authorization": "Bearer test-token"}
        controls = await self.client.get("/api/v1/controls", headers=headers)
        self.assertEqual(controls.status, 200)
        initial = (await controls.json())["data"]

        updated = await self.client.patch(
            "/api/v1/controls",
            headers=headers,
            json={
                "patch": {
                    "intervention": {
                        "mode": "hybrid",
                        "core_policy_mode": "deployment",
                        "core_temperature": 1.4,
                    }
                },
                "expected_revision": initial["revision"],
            },
        )

        self.assertEqual(updated.status, 200)
        payload = (await updated.json())["data"]
        self.assertEqual(payload["intervention"]["mode"], "hybrid")
        self.assertEqual(payload["intervention"]["core_policy_mode"], "deployment")
        self.assertEqual(payload["intervention"]["core_temperature"], 1.4)
        self.assertEqual(self.created, [])

    # 验证模型目录接口不会启动对局运行时
    async def test_model_catalog_is_available_while_stopped(self):
        response = await self.client.get(
            "/api/v1/models",
            headers={"Authorization": "Bearer test-token"},
        )

        self.assertEqual(response.status, 200)
        payload = (await response.json())["data"]
        self.assertEqual(payload["supported_model_protocols"], [3])
        self.assertIn("selection", payload)
        self.assertEqual(self.created, [])

    # 验证配置中心脱敏并能在停止状态保存下一次会话设置
    async def test_configuration_is_sanitized_and_editable_while_stopped(self):
        headers = {"Authorization": "Bearer test-token"}
        response = await self.client.get(
            "/api/v1/configuration",
            headers=headers,
        )
        self.assertEqual(response.status, 200)
        initial = (await response.json())["data"]
        self.assertNotIn("password", initial["server"])
        self.assertNotIn("api_key", initial["llm"])

        updated = await self.client.patch(
            "/api/v1/configuration",
            headers=headers,
            json={
                "patch": {
                    "server": {"trace_packets": False},
                    "agent": {"name": "Galatea_WebUI"},
                    "llm": {"temperature": 0.2},
                },
                "expected_revision": initial["revision"],
            },
        )

        self.assertEqual(updated.status, 200)
        payload = (await updated.json())["data"]
        self.assertFalse(payload["server"]["trace_packets"])
        self.assertEqual(payload["agent"]["name"], "Galatea_WebUI")
        self.assertEqual(payload["llm"]["temperature"], 0.2)
        self.assertEqual(self.created, [])

    # 验证 WebUI 可以保存和清除本地 LLM 密钥且响应不回显明文
    async def test_llm_api_key_endpoint_is_write_only(self):
        headers = {"Authorization": "Bearer test-token"}
        saved = await self.client.put(
            "/api/v1/configuration/llm-api-key",
            headers=headers,
            json={"action": "set", "api_key": "test-local-secret"},
        )
        self.assertEqual(saved.status, 200)
        saved_payload = (await saved.json())["data"]
        self.assertEqual(saved_payload["llm"]["api_key_source"], "local_file")
        self.assertNotIn("test-local-secret", str(saved_payload))

        cleared = await self.client.put(
            "/api/v1/configuration/llm-api-key",
            headers=headers,
            json={"action": "clear"},
        )
        self.assertEqual(cleared.status, 200)
        self.assertNotEqual(
            (await cleared.json())["data"]["llm"]["api_key_source"],
            "local_file",
        )

    # 验证 AstrBot 心跳会进入 Link 双向集成状态
    async def test_astrbot_heartbeat_is_visible_in_status(self):
        headers = {"Authorization": "Bearer test-token"}
        heartbeat = await self.client.post(
            "/api/v1/integrations/astrbot/heartbeat",
            headers=headers,
            json={
                "client_id": "astrbot-test-instance",
                "instance_name": "测试 AstrBot",
                "plugin_version": "1.6.0",
                "remote_agent_enabled": True,
                "action": "connect",
            },
        )
        self.assertEqual(heartbeat.status, 200)
        status = await self.client.get("/api/v1/status", headers=headers)
        integration = (await status.json())["data"]["integrations"]["astrbot"]
        self.assertTrue(integration["connected"])
        self.assertEqual(integration["clients"][0]["instance_name"], "测试 AstrBot")

    # 验证服务器连通测试只建立 TCP 连接且不会启动游戏运行时
    async def test_server_configuration_tcp_probe(self):
        async def accept_and_close(reader, writer):
            writer.close()
            await writer.wait_closed()

        probe_server = await asyncio.start_server(
            accept_and_close,
            "127.0.0.1",
            0,
        )
        port = probe_server.sockets[0].getsockname()[1]
        try:
            response = await self.client.post(
                "/api/v1/configuration/server/test",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "patch": {
                        "server": {
                            "host": "127.0.0.1",
                            "port": port,
                            "connect_timeout": 2.0,
                        }
                    }
                },
            )
        finally:
            probe_server.close()
            await probe_server.wait_closed()

        self.assertEqual(response.status, 200)
        payload = (await response.json())["data"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["scope"], "tcp_only")
        self.assertEqual(self.created, [])

    # 验证远程启动、控制、观察和聊天接口共用同一运行时
    async def test_session_control_contract(self):
        headers = {"Authorization": "Bearer test-token"}
        started = await self.client.post("/api/v1/session/start", headers=headers)
        self.assertEqual(started.status, 202)
        self.assertEqual(len(self.created), 1)

        controls = await self.client.patch(
            "/api/v1/controls",
            headers=headers,
            json={
                "patch": {"intervention": {"mode": "hybrid"}},
                "expected_revision": 0,
            },
        )
        self.assertEqual(controls.status, 200)
        controls_payload = await controls.json()
        self.assertEqual(
            controls_payload["data"]["baseline_intervention"]["mode"],
            "hybrid",
        )

        observation = await self.client.get("/api/v1/observation", headers=headers)
        self.assertEqual(observation.status, 200)
        chat = await self.client.post(
            "/api/v1/chat",
            headers=headers,
            json={"text": "你好"},
        )
        self.assertEqual(chat.status, 202)

        external = await self.client.post(
            "/api/v1/external-message",
            headers=headers,
            json={
                "text": "下一步保守一点",
                "source": "astrbot.qq",
                "sender_id": "user-1",
            },
        )
        self.assertEqual(external.status, 202)
        external_payload = (await external.json())["data"]
        self.assertEqual(external_payload["payload"]["source"], "astrbot.qq")

        pending = await self.client.get("/api/v1/decisions/pending", headers=headers)
        self.assertEqual(pending.status, 200)
        self.assertIsNone((await pending.json())["data"])
        submitted = await self.client.post(
            "/api/v1/decisions/12",
            headers=headers,
            json={"choice_id": 0},
        )
        self.assertEqual(submitted.status, 202)
        stopped = await self.client.post("/api/v1/session/stop", headers=headers)
        self.assertEqual(stopped.status, 200)

    # 验证远程会话配置接口接受临时密码但返回值始终脱敏
    async def test_ephemeral_session_configuration_api(self):
        headers = {"Authorization": "Bearer test-token"}
        response = await self.client.post(
            "/api/v1/session/configure",
            headers=headers,
            json={
                "server": {"password": "temporary-secret"},
                "agent": {"deck": "神秘白龙"},
                "decision": {"agent_backend": "remote_astrbot"},
            },
        )
        self.assertEqual(response.status, 200)
        payload = (await response.json())["data"]
        self.assertTrue(payload["session_configured"])
        self.assertNotIn("password", payload["server"])
        self.assertEqual(payload["decision"]["agent_backend"], "remote_astrbot")
        self.assertEqual(self.created, [])

    # 验证 WebSocket 可以持续接收结构化事件
    async def test_websocket_event_stream(self):
        headers = {"Authorization": "Bearer test-token"}
        websocket = await self.client.ws_connect(
            "/api/v1/events",
            headers=headers,
        )
        connected = await websocket.receive_json()
        self.assertEqual(
            connected["event"]["event_type"],
            "service.connected",
        )
        await self.client.post("/api/v1/session/start", headers=headers)

        event_types = []
        while "service.session.started" not in event_types:
            event = await asyncio.wait_for(websocket.receive_json(), timeout=2)
            event_types.append(event["event"]["event_type"])
        self.assertIn("service.session.started", event_types)
        await websocket.close()

    # 验证无令牌的非本机监听会在启动前失败
    async def test_public_bind_requires_token(self):
        with self.assertRaisesRegex(ValueError, "必须配置"):
            validate_service_security(
                ServiceConfig(host="0.0.0.0"),
                "",
            )


class DeckHttpApiTests(unittest.IsolatedAsyncioTestCase):
    # 创建位于临时目录的会话隔离卡组 API
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "decks").mkdir()
        (root / "decks/本地卡组.ydk").write_text(
            "#main\n89631139\n#extra\n!side\n",
            encoding="utf-8",
        )
        config_path = root / "config.yaml"
        config_path.write_text(
            "agent:\n  deck: 本地卡组\n",
            encoding="utf-8",
        )
        self.manager = LinkSessionManager(
            config_path,
            runtime_builder=lambda _: FakeLink(),
        )
        app = create_http_app(
            self.manager,
            ServiceConfig(api_token="deck-token"),
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    # 关闭临时服务并清理隔离文件
    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    # 验证当前配置卡组不能删除且运行中的本地卡组不能修改
    async def test_selected_delete_and_running_edit_are_rejected(self):
        headers = {"Authorization": "Bearer deck-token"}
        selected_delete = await self.client.delete(
            "/api/v1/decks/local",
            headers=headers,
            json={"deck_ref": "本地卡组"},
        )
        self.assertEqual(selected_delete.status, 409)

        stopped_edit = await self.client.patch(
            "/api/v1/decks/local",
            headers=headers,
            json={
                "deck_ref": "本地卡组",
                "operations": [
                    {"operation": "add", "code": 46986414, "section": "side"}
                ],
            },
        )
        self.assertEqual(stopped_edit.status, 200)

        await self.client.post("/api/v1/session/start", headers=headers)
        running_edit = await self.client.patch(
            "/api/v1/decks/local",
            headers=headers,
            json={
                "deck_ref": "本地卡组",
                "operations": [
                    {"operation": "remove", "code": 46986414, "section": "side"}
                ],
            },
        )
        self.assertEqual(running_edit.status, 409)

    # 验证 HTTP 接口只向请求会话返回本地卡组临时副本
    async def test_local_deck_copy_endpoint_creates_editable_session_copy(self):
        headers = {"Authorization": "Bearer deck-token"}
        response = await self.client.post(
            "/api/v1/decks/copy-local",
            headers=headers,
            json={
                "scope_id": "group_100",
                "deck_ref": "本地卡组",
                "instance_name": "AstrBot",
            },
        )

        self.assertEqual(response.status, 201)
        record = (await response.json())["data"]
        self.assertTrue(record["deck_ref"].startswith("astrbot:"))
        self.assertEqual(record["source"]["kind"], "link_local_copy")
        own = await self.client.post(
            "/api/v1/decks/query",
            headers=headers,
            json={"scope_id": "group_100"},
        )
        other = await self.client.post(
            "/api/v1/decks/query",
            headers=headers,
            json={"scope_id": "group_200"},
        )
        self.assertIn(
            record["deck_ref"],
            {item["deck_ref"] for item in (await own.json())["data"]["decks"]},
        )
        self.assertNotIn(
            record["deck_ref"],
            {item["deck_ref"] for item in (await other.json())["data"]["decks"]},
        )

    # 验证导入卡组只能由相同 AstrBot 会话查询和选择
    async def test_deck_import_query_and_selection_are_scope_isolated(self):
        headers = {"Authorization": "Bearer deck-token"}
        imported = await self.client.post(
            "/api/v1/decks/import",
            headers=headers,
            json={
                "scope_id": "group_100",
                "display_name": "当前群解析卡组",
                "instance_name": "AstrBot",
                "original_filename": "deck_group_100.ydk",
                "ydk_text": "#main\n89631139\n#extra\n23995346\n!side\n",
            },
        )
        self.assertEqual(imported.status, 201)
        record = (await imported.json())["data"]
        self.assertEqual(record["source"]["scope"], "current_session")
        self.assertNotIn("group_100", str(record))

        own = await self.client.post(
            "/api/v1/decks/query",
            headers=headers,
            json={"scope_id": "group_100"},
        )
        other = await self.client.post(
            "/api/v1/decks/query",
            headers=headers,
            json={"scope_id": "group_200"},
        )
        own_refs = {item["deck_ref"] for item in (await own.json())["data"]["decks"]}
        other_refs = {item["deck_ref"] for item in (await other.json())["data"]["decks"]}
        self.assertIn(record["deck_ref"], own_refs)
        self.assertNotIn(record["deck_ref"], other_refs)

        rejected = await self.client.post(
            "/api/v1/session/configure",
            headers=headers,
            json={
                "agent": {"deck": record["deck_ref"]},
                "deck_scope_id": "group_200",
            },
        )
        self.assertEqual(rejected.status, 400)
        accepted = await self.client.post(
            "/api/v1/session/configure",
            headers=headers,
            json={
                "agent": {"deck": record["deck_ref"]},
                "deck_scope_id": "group_100",
            },
        )
        self.assertEqual(accepted.status, 200)

        current = await self.client.get(
            "/api/v1/session/configuration",
            headers=headers,
        )
        current_data = (await current.json())["data"]
        self.assertEqual(current_data["agent"]["deck"], record["deck_ref"])
        self.assertNotIn("password", current_data["server"])

        wrong_edit = await self.client.patch(
            "/api/v1/decks/current",
            headers=headers,
            json={
                "scope_id": "group_200",
                "operations": [
                    {"operation": "add", "code": 46986414, "section": "main"}
                ],
            },
        )
        self.assertEqual(wrong_edit.status, 403)

        edited = await self.client.patch(
            "/api/v1/decks/current",
            headers=headers,
            json={
                "scope_id": "group_100",
                "operations": [
                    {"operation": "add", "code": 46986414, "section": "main"},
                    {
                        "operation": "move",
                        "code": 23995346,
                        "section": "extra",
                        "to_section": "side",
                    },
                ],
            },
        )
        edited_data = (await edited.json())["data"]
        self.assertEqual(edited.status, 200)
        self.assertEqual(edited_data["counts"], {"main": 2, "extra": 0, "side": 1})
        self.assertEqual(edited_data["source"]["modified_by"], "AstrBot 主智能体")

        started = await self.client.post(
            "/api/v1/session/start",
            headers=headers,
        )
        self.assertEqual(started.status, 202)
        running_edit = await self.client.patch(
            "/api/v1/decks/current",
            headers=headers,
            json={
                "scope_id": "group_100",
                "operations": [
                    {"operation": "remove", "code": 46986414, "section": "main"}
                ],
            },
        )
        self.assertEqual(running_edit.status, 409)


if __name__ == "__main__":
    unittest.main()
