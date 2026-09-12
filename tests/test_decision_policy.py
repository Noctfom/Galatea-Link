# LLM 介入策略测试，验证四种模式与置信度阈值路由

import unittest
from types import SimpleNamespace

from agents.decision_policy import InterventionPolicy
from app_config import DecisionConfig


class InterventionPolicyTests(unittest.TestCase):
    # 验证纯 Core 模式不会调用 LLM
    def test_core_only(self):
        policy = InterventionPolicy(DecisionConfig(mode="core_only"))
        core = SimpleNamespace(confidence=0.1)

        self.assertTrue(policy.should_run_core())
        self.assertFalse(policy.should_use_llm(16, core))

    # 验证纯 LLM 模式不会预先运行 Core
    def test_llm_only(self):
        policy = InterventionPolicy(DecisionConfig(mode="llm_only"))

        self.assertFalse(policy.should_run_core())
        self.assertTrue(policy.should_use_llm(11, None))

    # 验证审核模式总会把 Core 结果交给 LLM
    def test_llm_review(self):
        policy = InterventionPolicy(DecisionConfig(mode="llm_review"))
        core = SimpleNamespace(confidence=0.99)

        self.assertTrue(policy.should_run_core())
        self.assertTrue(policy.should_use_llm(11, core))

    # 验证混合模式按低置信度和指定时点介入
    def test_hybrid_routes_by_confidence_and_message_type(self):
        policy = InterventionPolicy(
            DecisionConfig(
                mode="hybrid",
                core_confidence_threshold=0.65,
                force_llm_message_types=(16,),
            )
        )
        confident = SimpleNamespace(confidence=0.9)
        uncertain = SimpleNamespace(confidence=0.4)

        self.assertFalse(policy.should_use_llm(11, confident))
        self.assertTrue(policy.should_use_llm(11, uncertain))
        self.assertTrue(policy.should_use_llm(16, confident))
