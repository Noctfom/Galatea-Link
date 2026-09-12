# LLM 固定提示上下文测试，验证卡组汇总和效果文本开关

import unittest

from core.llm_prompt_context import build_llm_static_context


class FakeCardReader:
    # 返回便于断言的卡名
    def get_card_name(self, code):
        return f"Card {code}"

    # 返回便于断言的效果文本
    def get_card_text(self, code):
        return f"Text {code}"


class LlmPromptContextTests(unittest.TestCase):
    # 验证重复卡密会汇总且效果文本可进入固定前缀
    def test_builds_counted_static_deck_context(self):
        context = build_llm_static_context(
            [2, 1, 2],
            [3],
            deck_name="测试卡组",
            agent_name="测试智能体",
            include_card_text=True,
            card_reader=FakeCardReader(),
        )

        self.assertEqual(
            context["own_initial_deck"]["main"],
            [
                {"code": 1, "name": "Card 1", "count": 1, "text": "Text 1"},
                {"code": 2, "name": "Card 2", "count": 2, "text": "Text 2"},
            ],
        )
        self.assertEqual(context["own_initial_deck"]["extra"][0]["code"], 3)


if __name__ == "__main__":
    unittest.main()
