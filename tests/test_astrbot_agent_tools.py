# AstrBot 游戏王智能体工具测试，验证卡名卡密详情和裁定查询结构

import unittest

from astrbot_plugin_duel_galatea_stage.galatea_agent_tools import (
    lookup_card_for_agent,
)


class FakeCardSearcher:
    # 初始化固定卡片数据和调用记录
    def __init__(self):
        self.search_calls = []
        self.detail_calls = []

    # 返回包含模糊候选和精确候选的搜索结果
    async def search_card(self, query):
        self.search_calls.append(query)
        return {
            "result": [
                {"id": 2, "cn_name": "青眼亚白龙"},
                {"id": 1, "cn_name": "青眼白龙"},
            ]
        }

    # 返回指定卡密的完整卡片详情
    async def get_card_detail(self, card_id):
        self.detail_calls.append(str(card_id))
        names = {"1": "青眼白龙", "2": "青眼亚白龙"}
        return {
            "id": int(card_id),
            "cn_name": names[str(card_id)],
            "sc_name": names[str(card_id)],
            "text": {
                "types": "龙族/通常",
                "desc": f"卡片 {card_id} 的效果文本",
                "pdesc": "",
            },
            "data": {"atk": 3000, "def": 2500, "level": 8},
        }

    # 返回用于裁定解析的测试页面内容
    async def get_card_html(self, card_id):
        return f"html:{card_id}"

    # 返回固定裁定问答
    def parse_card_faq(self, html_text):
        return [{"title": "处理方法", "q": "如何处理", "a": "按文本处理"}]


class AstrBotAgentCardToolTests(unittest.IsolatedAsyncioTestCase):
    # 验证卡名查询优先返回精确匹配并附带裁定
    async def test_name_lookup_prefers_exact_match_and_returns_rulings(self):
        searcher = FakeCardSearcher()

        result = await lookup_card_for_agent(
            searcher,
            "青眼白龙",
            include_rulings=True,
            max_results=2,
        )

        self.assertEqual(result["match_mode"], "name")
        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(result["cards"][0]["card_id"], "1")
        self.assertEqual(result["cards"][0]["effect_text"], "卡片 1 的效果文本")
        self.assertEqual(result["rulings"]["card_id"], "1")
        self.assertEqual(result["rulings"]["items"][0]["answer"], "按文本处理")

    # 验证数字卡密直接读取详情且不会执行名称搜索
    async def test_numeric_lookup_reads_card_detail_directly(self):
        searcher = FakeCardSearcher()

        result = await lookup_card_for_agent(searcher, "1")

        self.assertEqual(result["match_mode"], "card_id")
        self.assertEqual(result["cards"][0]["cn_name"], "青眼白龙")
        self.assertEqual(searcher.search_calls, [])
        self.assertEqual(searcher.detail_calls, ["1"])


if __name__ == "__main__":
    unittest.main()
