# 应用配置测试，验证 MDPro3 协议配置的解析和校验

import importlib.util
import tempfile
import unittest
from pathlib import Path

from app_config import (
    LINK_PROJECT_ROOT,
    build_app_config,
    load_app_config,
    resolve_link_resource_path,
)


class AppConfigTests(unittest.TestCase):
    # 验证十六进制协议版本和基础设置可以正确解析
    def test_build_config_from_mapping(self):
        config = build_app_config(
            {
                "server": {
                    "profile": "mdpro3",
                    "host": "192.0.2.10",
                    "port": "7911",
                    "protocol_version": "0x1361",
                    "auto_negotiate_version": True,
                    "max_version_retries": 2,
                    "game_id": 7,
                    "connect_timeout": 3.5,
                    "trace_packets": True,
                },
                "agent": {
                    "name": "Galatea_Test",
                    "deck": "测试卡组",
                    "prefer_second": True,
                },
                "model": {
                    "device": "cpu",
                    "weights_path": "model.pth",
                    "expected_model_id": "00000000-0000-0000-0000-000000000001",
                    "protocol": "v3",
                    "inference_backend": "onnxruntime",
                    "onnx_providers": ["CPUExecutionProvider"],
                    "onnx_intra_op_threads": 2,
                    "assets_path": "model_assets/v3",
                    "strict_asset_hashes": False,
                    "config": {"d_model": 256},
                },
                "llm": {
                    "enabled": True,
                    "base_url": "https://llm.example/v1",
                    "model": "test-model",
                    "max_tokens": 256,
                    "response_format": "json_schema",
                    "trace_requests": False,
                    "thinking_mode": "disabled",
                    "cache_deck_text": True,
                },
                "decision": {
                    "mode": "hybrid",
                    "agent_backend": "remote_astrbot",
                    "core_policy_mode": "deployment",
                    "core_temperature": 1.25,
                    "core_confidence_threshold": 0.7,
                    "force_llm_message_types": [13, "0x10"],
                    "llm_time_budget": 8.0,
                    "autonomous_intervention_enabled": True,
                    "autonomous_allowed_modes": ["hybrid", "llm_review"],
                    "autonomous_core_confidence_min": 0.3,
                    "autonomous_core_confidence_max": 0.8,
                    "autonomous_max_ttl_decisions": 2,
                    "autonomous_max_force_message_types": 4,
                },
                "service": {
                    "host": "0.0.0.0",
                    "port": 9876,
                    "api_token_env": "TEST_LINK_TOKEN",
                    "event_queue_size": 512,
                    "webui_enabled": False,
                    "allowed_origins": ["https://console.example"],
                },
            }
        )

        self.assertEqual(config.server.profile, "mdpro3")
        self.assertEqual(config.server.host, "192.0.2.10")
        self.assertEqual(config.server.port, 7911)
        self.assertEqual(config.server.protocol_version, 0x1361)
        self.assertTrue(config.server.auto_negotiate_version)
        self.assertEqual(config.server.max_version_retries, 2)
        self.assertEqual(config.server.game_id, 7)
        self.assertEqual(config.server.connect_timeout, 3.5)
        self.assertTrue(config.server.trace_packets)
        self.assertTrue(config.agent.prefer_second)
        self.assertEqual(config.model.config["d_model"], 256)
        self.assertEqual(
            config.model.expected_model_id,
            "00000000-0000-0000-0000-000000000001",
        )
        self.assertEqual(config.model.protocol, "v3")
        self.assertEqual(config.model.inference_backend, "onnxruntime")
        self.assertEqual(
            config.model.onnx_providers,
            ("CPUExecutionProvider",),
        )
        self.assertEqual(config.model.onnx_intra_op_threads, 2)
        self.assertEqual(config.model.assets_path, "model_assets/v3")
        self.assertFalse(config.model.strict_asset_hashes)
        self.assertTrue(config.llm.enabled)
        self.assertEqual(config.llm.model, "test-model")
        self.assertEqual(config.llm.response_format, "json_schema")
        self.assertFalse(config.llm.trace_requests)
        self.assertEqual(config.llm.thinking_mode, "disabled")
        self.assertTrue(config.llm.cache_deck_text)
        self.assertEqual(config.decision.mode, "hybrid")
        self.assertEqual(config.decision.agent_backend, "remote_astrbot")
        self.assertEqual(config.decision.core_policy_mode, "deployment")
        self.assertEqual(config.decision.core_temperature, 1.25)
        self.assertEqual(config.decision.force_llm_message_types, (13, 16))
        self.assertEqual(config.decision.llm_time_budget, 8.0)
        self.assertTrue(config.decision.autonomous_intervention_enabled)
        self.assertEqual(
            config.decision.autonomous_allowed_modes,
            ("hybrid", "llm_review"),
        )
        self.assertEqual(config.decision.autonomous_max_ttl_decisions, 2)
        self.assertEqual(config.service.host, "0.0.0.0")
        self.assertEqual(config.service.port, 9876)
        self.assertEqual(config.service.api_token_env, "TEST_LINK_TOKEN")
        self.assertEqual(config.service.event_queue_size, 512)
        self.assertFalse(config.service.webui_enabled)
        self.assertEqual(
            config.service.allowed_origins,
            ("https://console.example",),
        )

    # 验证含义不明的布尔配置会被拒绝
    def test_rejects_invalid_boolean(self):
        with self.assertRaisesRegex(ValueError, "必须是布尔值"):
            build_app_config({"agent": {"prefer_second": "sometimes"}})

    # 验证不支持的结构化输出模式会被拒绝
    def test_rejects_invalid_llm_response_format(self):
        with self.assertRaisesRegex(ValueError, "response_format"):
            build_app_config({"llm": {"response_format": "xml"}})

    # 验证不支持的思考模式会在启动前被拒绝
    def test_rejects_invalid_thinking_mode(self):
        with self.assertRaisesRegex(ValueError, "thinking_mode"):
            build_app_config({"llm": {"thinking_mode": "sometimes"}})

    # 验证未知模型协议固定值会在启动前被拒绝
    def test_rejects_invalid_model_protocol(self):
        with self.assertRaisesRegex(ValueError, "model.protocol"):
            build_app_config({"model": {"protocol": "v9"}})

    # 验证未知推理后端会在加载模型前被拒绝
    def test_rejects_invalid_inference_backend(self):
        with self.assertRaisesRegex(ValueError, "inference_backend"):
            build_app_config({"model": {"inference_backend": "tensorflow"}})

    # 验证独立服务端口和事件队列范围会在启动前校验
    def test_rejects_invalid_service_ranges(self):
        with self.assertRaisesRegex(ValueError, "service.port"):
            build_app_config({"service": {"port": 70000}})
        with self.assertRaisesRegex(ValueError, "event_queue_size"):
            build_app_config({"service": {"event_queue_size": 2}})

    # 验证 Core 置信度阈值必须位于概率范围
    def test_rejects_invalid_confidence_threshold(self):
        with self.assertRaisesRegex(ValueError, "confidence_threshold"):
            build_app_config(
                {"decision": {"core_confidence_threshold": 1.5}}
            )

    # 验证 Core 部署策略和温度在加载阶段受到约束
    def test_validates_core_policy_and_temperature(self):
        config = build_app_config(
            {
                "model": {"protocol": "core-3.6.5"},
                "decision": {
                    "core_policy_mode": "deployment",
                    "core_temperature": 1.5,
                },
            }
        )
        self.assertEqual(config.model.protocol, "core-3.6.5")
        self.assertEqual(config.decision.core_policy_mode, "deployment")
        self.assertEqual(config.decision.core_temperature, 1.5)
        with self.assertRaisesRegex(ValueError, "core_policy_mode"):
            build_app_config({"decision": {"core_policy_mode": "random"}})
        with self.assertRaisesRegex(ValueError, "core_temperature"):
            build_app_config({"decision": {"core_temperature": 0.0}})

    # 验证 LLM 决策时间预算必须为正数
    def test_rejects_invalid_llm_time_budget(self):
        with self.assertRaisesRegex(ValueError, "llm_time_budget"):
            build_app_config({"decision": {"llm_time_budget": 0}})

    # 验证自主介入护栏必须使用合法范围
    def test_rejects_invalid_autonomous_guardrails(self):
        with self.assertRaisesRegex(ValueError, "autonomous_allowed_modes"):
            build_app_config(
                {"decision": {"autonomous_allowed_modes": ["random"]}}
            )
        with self.assertRaisesRegex(ValueError, "置信度范围"):
            build_app_config(
                {
                    "decision": {
                        "autonomous_core_confidence_min": 0.9,
                        "autonomous_core_confidence_max": 0.2,
                    }
                }
            )

    # 验证外部进程启动时相对资源仍定位到 Link 项目目录
    def test_relative_resource_path_uses_link_project_root(self):
        resolved = resolve_link_resource_path("models/example.pth")

        self.assertEqual(
            resolved,
            str((LINK_PROJECT_ROOT / "models/example.pth").resolve()),
        )

    # 验证真实 YAML 文件可以加载为应用配置
    @unittest.skipUnless(importlib.util.find_spec("yaml"), "需要 PyYAML")
    def test_load_yaml_file(self):
        yaml_text = """
server:
  profile: mdpro3
  protocol_version: 0x1361
agent:
  prefer_second: false
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(yaml_text, encoding="utf-8")
            config = load_app_config(config_path)

        self.assertEqual(config.server.protocol_version, 0x1361)
        self.assertFalse(config.agent.prefer_second)


if __name__ == "__main__":
    unittest.main()
