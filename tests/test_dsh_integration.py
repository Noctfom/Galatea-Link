# dsh 接入测试，覆盖后端校验、心跳租约与会话卡组隐私

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from app_config import ServiceConfig, build_app_config
from core.link_events import LinkEventBus
from core.remote_decisions import RemoteDecisionBroker
from core.runtime_controls import apply_decision_control_patch
from service.http_api import create_http_app
from service.session_manager import LinkSessionManager
from service.external_integrations import ExternalIntegrationRegistry
from service.deck_repository import LinkDeckRepository


YDK = "#main\n89631139\n#extra\n23995346\n!side\n46986414\n"


class DshIntegrationTests(unittest.IsolatedAsyncioTestCase):
    # 验证 dsh 后端在动态控制层保留混合模式
    async def test_dsh_backend_keeps_hybrid(self):
        config = build_app_config({"decision": {"mode": "hybrid", "agent_backend": "remote_dsh"}})
        updated = apply_decision_control_patch(config.decision, {"intervention": {"agent_backend": "remote_astrbot"}})
        self.assertEqual(updated.mode, "hybrid")

    # 验证 dsh 决策拒绝缺失后端与重复提交
    async def test_decision_backend_and_expiry(self):
        events = LinkEventBus()
        broker = RemoteDecisionBroker(events)
        observation = {"observation_id": 2, "legal_actions": [{"choice_id": 0}]}
        task = asyncio.create_task(broker.decide(1, observation, 1, backend="remote_dsh"))
        await asyncio.sleep(0)
        submission = {"session_id": broker.session_id, "observation_id": 2, "choice_id": 0}
        try:
            with self.assertRaisesRegex(RuntimeError, "后端不匹配"):
                broker.submit(1, submission)
            broker.submit(1, {**submission, "agent_backend": "remote_dsh"})
            self.assertEqual((await task).choice_id, 0)
            with self.assertRaisesRegex(RuntimeError, "过期"):
                broker.submit(1, {**submission, "agent_backend": "remote_dsh"})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            events.close()

    # 验证心跳租约自动失效
    async def test_heartbeat_expiry(self):
        events = LinkEventBus()
        registry = ExternalIntegrationRegistry("dsh", events)
        with patch("service.external_integrations.time.time", return_value=100):
            registry.record({"client_id": "dsh-test"})
            self.assertTrue(registry.status()["connected"])
        with patch("service.external_integrations.time.time", return_value=161):
            self.assertFalse(registry.status()["connected"])
        events.close()

    # 验证 HTTP 心跳鉴权和能力声明
    async def test_http_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text("{}", encoding="utf-8")
            manager = LinkSessionManager(config_path, runtime_builder=lambda path: None)
            client = TestClient(TestServer(create_http_app(manager, ServiceConfig(), api_token="test-token")))
            await client.start_server()
            try:
                route = "/api/v1/integrations/dsh/heartbeat"
                self.assertEqual((await client.post(route, json={"client_id": "test"})).status, 401)
                response = await client.post(route, json={"client_id": "test"}, headers={"Authorization": "Bearer test-token"})
                self.assertEqual(response.status, 200)
                self.assertTrue(manager.get_status()["integrations"]["dsh"]["connected"])
                self.assertFalse(manager.get_status()["integrations"]["astrbot"]["connected"])
                capabilities = await (await client.get("/api/v1/capabilities")).json()
                self.assertIn("remote_dsh", capabilities["data"]["remote_agent_decisions"]["backends"])
            finally:
                await client.close()

    # 验证 dsh 卡组来源标记与跨会话隔离
    async def test_deck_source_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = LinkDeckRepository(Path(directory))
            record = repository.import_astrbot_ydk({"scope_id": "dsh:a", "display_name": "测试", "ydk_text": YDK, "source_provider": "dsh"})
            self.assertEqual(record["source"]["provider"], "dsh")
            self.assertFalse(repository.discover(scope_id="dsh:b"))
            updated = repository.edit_astrbot_deck("dsh:a", record["deck_ref"], [{"operation": "add", "section": "main", "code": 46986414}])
            self.assertEqual(updated["source"]["modified_by"], "dsh 主智能体")
            with self.assertRaises(PermissionError):
                repository.edit_astrbot_deck("dsh:b", record["deck_ref"], [])


if __name__ == "__main__":
    unittest.main()
