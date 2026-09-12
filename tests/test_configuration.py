# Link 配置中心测试，验证草稿校验、脱敏快照和最小 LLM 测试护栏

import unittest

from agents.llm_smoke import run_llm_smoke_test
from app_config import AgentConfig, LlmConfig, ServerConfig, ServiceConfig
from service.configuration import apply_configuration_patch, build_configuration_snapshot


class LinkConfigurationTests(unittest.IsolatedAsyncioTestCase):
    # 验证十六进制协议值和非敏感 LLM 草稿可以正确应用
    async def test_applies_configuration_patch(self):
        server, agent, llm = apply_configuration_patch(
            ServerConfig(),
            AgentConfig(),
            LlmConfig(),
            {
                "server": {
                    "protocol_version": "0x1361",
                    "auto_negotiate_version": True,
                    "max_version_retries": 2,
                    "port": 7912,
                },
                "agent": {"name": "Galatea_Test", "deck": "测试卡组"},
                "llm": {
                    "enabled": True,
                    "base_url": "https://llm.example/v1",
                    "model": "test-model",
                    "temperature": 0.3,
                },
            },
        )

        self.assertEqual(server.protocol_version, 0x1361)
        self.assertEqual(server.port, 7912)
        self.assertTrue(server.auto_negotiate_version)
        self.assertEqual(server.max_version_retries, 2)
        self.assertEqual(agent.name, "Galatea_Test")
        self.assertEqual(llm.model, "test-model")
        self.assertEqual(llm.temperature, 0.3)

    # 验证配置快照只返回密钥状态而不返回明文秘密
    async def test_snapshot_hides_secret_values(self):
        snapshot = build_configuration_snapshot(
            ServerConfig(password="duel-secret"),
            AgentConfig(),
            LlmConfig(api_key="llm-secret"),
            ServiceConfig(api_token="service-secret"),
            2,
        )

        serialized = str(snapshot)
        self.assertNotIn("duel-secret", serialized)
        self.assertNotIn("llm-secret", serialized)
        self.assertNotIn("service-secret", serialized)
        self.assertTrue(snapshot["server"]["password_configured"])
        self.assertTrue(snapshot["llm"]["api_key_configured"])
        self.assertTrue(snapshot["service"]["api_token_configured"])

    # 验证未启用 LLM 时冒烟测试不会产生网络请求
    async def test_disabled_llm_smoke_test_fails_locally(self):
        with self.assertRaisesRegex(RuntimeError, "启用 LLM"):
            await run_llm_smoke_test(LlmConfig(enabled=False))

    # 验证配置中心拒绝含凭据 URL 和过长 Agent 名称
    async def test_rejects_unsafe_configuration(self):
        with self.assertRaisesRegex(ValueError, "内嵌凭据"):
            apply_configuration_patch(
                ServerConfig(),
                AgentConfig(),
                LlmConfig(),
                {"llm": {"base_url": "https://user:pass@example.com/v1"}},
            )
        with self.assertRaisesRegex(ValueError, "UTF-16"):
            apply_configuration_patch(
                ServerConfig(),
                AgentConfig(),
                LlmConfig(),
                {"agent": {"name": "A" * 21}},
            )
        with self.assertRaisesRegex(ValueError, "自动重试次数"):
            apply_configuration_patch(
                ServerConfig(),
                AgentConfig(),
                LlmConfig(),
                {"server": {"max_version_retries": 4}},
            )


if __name__ == "__main__":
    unittest.main()
