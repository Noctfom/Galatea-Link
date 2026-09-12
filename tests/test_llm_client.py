# LLM 客户端测试，验证异步请求、密钥读取和非法动作拦截

import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import httpx

from agents.llm_client import (
    LlmClientError,
    LlmDecisionSkipped,
    OpenAICompatibleLlmClient,
)
from app_config import LlmConfig


# 创建最小可决策观察
def make_observation():
    return {
        "schema_version": "galatea.llm_observation.v1",
        "decision_required": True,
        "legal_actions": [
            {"choice_id": 0, "action_name": "normal_summon"},
            {"choice_id": 1, "action_name": "enter_end_phase"},
        ],
    }


class LlmClientTests(unittest.IsolatedAsyncioTestCase):
    # 验证请求采用配置值并解析合法结构化动作
    async def test_requests_and_parses_valid_decision(self):
        captured = {}

        # 模拟兼容服务端并记录收到的请求
        def handler(request):
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("Authorization")
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"choice_id":1,"reason":"结束回合","chat_message":null}'
                            }
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            config = LlmConfig(
                enabled=True,
                base_url="https://llm.example/v1/",
                api_key_env="TEST_GALATEA_LLM_KEY",
                model="test-model",
            )
            client = OpenAICompatibleLlmClient(config, http_client=http_client)
            with patch.dict(os.environ, {"TEST_GALATEA_LLM_KEY": "secret"}):
                decision = await client.decide(make_observation())

        self.assertEqual(decision.choice_id, 1)
        self.assertEqual(decision.reason, "结束回合")
        self.assertEqual(captured["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer secret")
        self.assertEqual(captured["payload"]["model"], "test-model")
        self.assertEqual(captured["payload"]["response_format"]["type"], "json_object")

    # 验证 Markdown 包装可以解析但越界动作仍会被拒绝
    async def test_rejects_unknown_choice_id(self):
        # 返回带代码块包装的非法动作
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "```json\n{\"choice_id\":9,\"reason\":\"x\",\"chat_message\":null}\n```"
                            }
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAICompatibleLlmClient(
                LlmConfig(enabled=True, model="test-model"),
                http_client=http_client,
            )
            with self.assertRaisesRegex(LlmClientError, "非法 choice_id"):
                await client.decide(make_observation())

    # 验证推理标签和解释文字不会阻止提取最终 JSON
    async def test_extracts_json_after_reasoning_text(self):
        # 返回常见推理模型包装格式
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "<think>先分析局面</think>\n"
                                    "最终选择如下\n"
                                    "```json\n"
                                    '{"choice_id":0,"reason":"通常召唤","chat_message":null}'
                                    "\n```"
                                )
                            }
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAICompatibleLlmClient(
                LlmConfig(enabled=True, model="test-model"),
                http_client=http_client,
            )
            decision = await client.decide(make_observation())

        self.assertEqual(decision.choice_id, 0)
        self.assertEqual(decision.reason, "通常召唤")

    # 验证无效 JSON 错误包含受限长度的原始内容
    async def test_invalid_json_includes_response_preview(self):
        # 返回完全没有 JSON 的文本
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "我无法选择这个动作"},
                            "finish_reason": "length",
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAICompatibleLlmClient(
                LlmConfig(enabled=True, model="test-model"),
                http_client=http_client,
            )
            with self.assertRaisesRegex(LlmClientError, "我无法选择这个动作"):
                await client.decide(make_observation())
            with self.assertRaisesRegex(LlmClientError, "finish_reason.*length"):
                await client.decide(make_observation())

    # 验证网络超时具有独立错误分类
    async def test_reports_timeout_separately(self):
        # 模拟读取模型响应时超时
        def handler(request):
            raise httpx.ReadTimeout("slow response", request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAICompatibleLlmClient(
                LlmConfig(enabled=True, model="test-model", timeout=2.0),
                http_client=http_client,
            )
            with self.assertRaisesRegex(LlmClientError, "请求超时.*2.0 秒"):
                await client.decide(make_observation())

    # 验证没有决策时点时不会调用远端接口
    async def test_rejects_non_decision_observation(self):
        # 如果意外发起请求则让测试立即失败
        def handler(request):
            raise AssertionError("不应调用 LLM API")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAICompatibleLlmClient(
                LlmConfig(enabled=True, model="test-model"),
                http_client=http_client,
            )
            observation = make_observation()
            observation["decision_required"] = False
            with self.assertRaisesRegex(LlmDecisionSkipped, "没有可提交"):
                await client.decide(observation)

    # 验证固定前缀保持独立并压缩重复动态卡片字段
    async def test_static_context_and_dynamic_compaction(self):
        captured = {}

        # 记录发送给兼容服务端的消息数组
        def handler(request):
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"choice_id":0,"reason":"测试","chat_message":null}'
                            }
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAICompatibleLlmClient(
                LlmConfig(
                    enabled=True,
                    model="test-model",
                    thinking_mode="disabled",
                ),
                http_client=http_client,
            )
            client.set_static_context(
                {
                    "own_initial_deck": {
                        "main": [{"code": 123, "name": "测试卡", "text": "测试效果"}],
                        "extra": [],
                    }
                }
            )
            observation = make_observation()
            observation["known_information"] = {
                "own_remaining_deck": [{"code": 123, "name": "测试卡", "count": 2}]
            }
            observation["card_catalog"] = [
                {"code": 123, "name": "测试卡", "text": "测试效果"}
            ]
            await client.decide(observation)

        messages = captured["payload"]["messages"]
        dynamic = json.loads(messages[-1]["content"])
        self.assertEqual(len(messages), 3)
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(dynamic["card_catalog"], [])
        self.assertEqual(
            dynamic["known_information"]["own_remaining_deck"],
            [{"code": 123, "count": 2}],
        )
        self.assertEqual(observation["card_catalog"][0]["text"], "测试效果")

    # 验证 DeepSeek 与 OpenAI 风格缓存统计都可识别
    async def test_logs_cache_usage(self):
        # 返回含 DeepSeek 缓存字段的模拟响应
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"choice_id":0,"reason":"测试","chat_message":null}'
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "prompt_cache_hit_tokens": 80,
                        "prompt_cache_miss_tokens": 20,
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAICompatibleLlmClient(
                LlmConfig(enabled=True, model="test-model"),
                http_client=http_client,
            )
            output = StringIO()
            with redirect_stdout(output):
                await client.decide(make_observation())

        self.assertIn("cache_hit_tokens=80", output.getvalue())
        self.assertIn("cache_miss_tokens=20", output.getvalue())
        self.assertIn("cache_hit_rate=80.0%", output.getvalue())

    # 验证合法的宏观介入调整会随动作结果一并解析
    async def test_parses_autonomous_intervention_update(self):
        # 返回带临时介入调整的结构化动作
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "choice_id": 1,
                                        "reason": "结束回合",
                                        "chat_message": None,
                                        "intervention_update": {
                                            "mode": "hybrid",
                                            "core_confidence_threshold": 0.5,
                                            "force_llm_message_types": [13, 16, 13],
                                            "ttl_decisions": 2,
                                            "reason": "后续两个时点观察 Core 置信度",
                                            "base_revision": 4,
                                        },
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = OpenAICompatibleLlmClient(
                LlmConfig(enabled=True, model="test-model"),
                http_client=http_client,
            )
            decision = await client.decide(make_observation())

        self.assertEqual(decision.intervention_update.mode, "hybrid")
        self.assertEqual(
            decision.intervention_update.force_llm_message_types,
            (13, 16),
        )
        self.assertEqual(decision.intervention_update.ttl_decisions, 2)
        self.assertEqual(decision.intervention_update.base_revision, 4)

    # 验证空白或越界的宏观介入调整会被拒绝
    def test_rejects_invalid_intervention_update(self):
        client = OpenAICompatibleLlmClient.__new__(OpenAICompatibleLlmClient)
        with self.assertRaisesRegex(LlmClientError, "没有任何有效字段"):
            client._parse_intervention_update(
                {
                    "mode": None,
                    "core_confidence_threshold": None,
                    "force_llm_message_types": None,
                    "ttl_decisions": 1,
                    "reason": "无调整",
                    "base_revision": 0,
                }
            )
        with self.assertRaisesRegex(LlmClientError, "0 到 255"):
            client._parse_intervention_update(
                {
                    "mode": "hybrid",
                    "force_llm_message_types": [999],
                    "ttl_decisions": 1,
                    "base_revision": 0,
                }
            )

    # 验证 OpenAI 风格的嵌套缓存字段也可被识别
    def test_extracts_nested_cached_tokens(self):
        client = OpenAICompatibleLlmClient.__new__(OpenAICompatibleLlmClient)
        metrics = client._extract_usage_metrics(
            {
                "input_tokens": 200,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 150},
                "output_tokens_details": {"reasoning_tokens": 2},
            }
        )

        self.assertEqual(metrics["cache_hit_tokens"], 150)
        self.assertEqual(metrics["cache_miss_tokens"], 50)
        self.assertEqual(metrics["cache_hit_rate"], "75.0%")
        self.assertEqual(metrics["reasoning_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
