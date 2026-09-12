# Link 设置存储测试，验证离线控制持久化且敏感配置不会写入状态文件

import json
import tempfile
import unittest
from pathlib import Path

from service.settings_store import LinkServiceSettingsStore


class LinkServiceSettingsStoreTests(unittest.TestCase):
    # 验证策略和模型选择可恢复且状态文件不包含密钥或服务器密码
    def test_persists_only_non_sensitive_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                """
server:
  password: duel-secret
llm:
  api_key: llm-secret
service:
  api_token: service-secret
decision:
  mode: core_only
""",
                encoding="utf-8",
            )
            store = LinkServiceSettingsStore(config_path)
            decision, game_chat = store.preview_controls(
                {
                    "intervention": {
                        "mode": "hybrid",
                        "core_policy_mode": "deployment",
                        "core_temperature": 1.3,
                    }
                },
                expected_revision=0,
            )
            store.commit_controls(decision, game_chat)
            store.select_model(
                {
                    "primary": "galatea_iter_3.onnx",
                    "format": "onnx",
                    "model_id": "00000000-0000-0000-0000-000000000303",
                    "model_protocol_version": 3,
                },
                assets_path="./model_assets/v3/test",
                expected_revision=0,
            )

            raw_state = (root / "link_state.json").read_text(encoding="utf-8")
            payload = json.loads(raw_state)
            restored = LinkServiceSettingsStore(config_path)

            self.assertNotIn("llm-secret", raw_state)
            self.assertNotIn("duel-secret", raw_state)
            self.assertNotIn("service-secret", raw_state)
            self.assertEqual(set(payload), {
                "schema_version",
                "controls_revision",
                "model_revision",
                "configuration_revision",
                "server",
                "agent",
                "llm",
                "decision",
                "game_chat",
                "model",
            })
            self.assertNotIn("password", payload["server"])
            self.assertNotIn("api_key", payload["llm"])
            self.assertEqual(restored.get_controls()["intervention"]["mode"], "hybrid")
            self.assertEqual(
                restored.get_controls()["intervention"]["core_temperature"],
                1.3,
            )
            self.assertEqual(
                restored.get_model_selection()["weights_path"],
                "./models/galatea_iter_3.onnx",
            )

    # 验证 LLM API Key 只写入独立密钥文件且普通状态始终脱敏
    def test_persists_llm_api_key_in_local_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "agent:\n  deck: 测试卡组\nllm:\n  base_url: https://llm.example/v1\n",
                encoding="utf-8",
            )
            store = LinkServiceSettingsStore(config_path)
            snapshot = store.set_llm_api_key("local-secret-value")

            self.assertTrue(snapshot["llm"]["api_key_configured"])
            self.assertEqual(snapshot["llm"]["api_key_source"], "local_file")
            self.assertNotIn("api_key", snapshot["llm"])
            self.assertFalse((root / "link_state.json").exists())
            secret_text = (root / "link_secrets.json").read_text(encoding="utf-8")
            self.assertIn("local-secret-value", secret_text)

            restored = LinkServiceSettingsStore(config_path)
            self.assertEqual(restored.get_app_config().llm.api_key, "local-secret-value")
            restored.set_llm_api_key(None)
            self.assertFalse((root / "link_secrets.json").exists())


if __name__ == "__main__":
    unittest.main()
