# AstrBot 工具集兼容测试，验证新版权限代理和旧版查询接口

import unittest

from astrbot_plugin_duel_galatea_stage.astrbot_tool_compat import (
    build_named_tool_set,
    build_plain_chat_context,
    is_llm_failure_response,
)


class FakeTool:
    # 初始化带名称的测试工具
    def __init__(self, name):
        self.name = name


class FakeToolSet:
    # 初始化最小工具集合
    def __init__(self, tools=None):
        self.tools = list(tools or [])

    # 添加或替换同名测试工具
    def add_tool(self, tool):
        self.tools = [item for item in self.tools if item.name != tool.name]
        self.tools.append(tool)

    # 按名称查询测试工具
    def get_tool(self, name):
        return next((tool for tool in self.tools if tool.name == name), None)


class ModernManager:
    # 初始化包含权限包装工具的新版管理器替身
    def __init__(self):
        self.guarded = FakeTool("submit")

    # 返回模拟新版权限代理的完整工具集
    def get_full_tool_set(self):
        return FakeToolSet([self.guarded, FakeTool("chat")])

    # 禁止测试误用会绕过权限包装的旧查询路径
    def get_func(self, name):
        raise AssertionError("不应绕过新版完整工具集")


class LegacyManager:
    # 使用旧版 get_func 接口返回工具
    def get_func(self, name):
        return FakeTool(name) if name == "submit" else None


class AstrBotToolCompatTests(unittest.TestCase):
    # 验证新版路径保留完整工具集中的权限代理对象
    def test_modern_manager_preserves_guarded_tools(self):
        manager = ModernManager()

        selected = build_named_tool_set(manager, FakeToolSet, ["submit"])

        self.assertIs(selected.get_tool("submit"), manager.guarded)

    # 验证旧版 get_func 路径仍可构建工具集
    def test_legacy_manager_uses_get_func(self):
        selected = build_named_tool_set(
            LegacyManager(),
            FakeToolSet,
            ["submit"],
        )

        self.assertEqual(selected.get_tool("submit").name, "submit")

    # 验证必需工具缺失时立即给出可定位错误
    def test_missing_tool_fails_with_name(self):
        with self.assertRaisesRegex(RuntimeError, "chat"):
            build_named_tool_set(
                LegacyManager(),
                FakeToolSet,
                ["submit", "chat"],
            )

    # 验证游戏聊天上下文会移除工具协议字段并跳过工具结果
    def test_plain_chat_context_removes_tool_protocol(self):
        history = [
            {"role": "user", "content": "帮我查卡"},
            {
                "role": "assistant",
                "content": "我看一下",
                "tool_calls": [{"id": "call-1", "function": {"name": "lookup"}}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "查询结果"},
            {"role": "assistant", "content": "这张卡可以发动"},
        ]

        context = build_plain_chat_context(history)

        self.assertEqual(
            context,
            [
                {"role": "user", "content": "帮我查卡"},
                {"role": "assistant", "content": "我看一下"},
                {"role": "assistant", "content": "这张卡可以发动"},
            ],
        )
        self.assertNotIn("tool_calls", repr(context))

    # 验证模型错误不会进入游戏聊天上下文或作为回复发送
    def test_filters_astrbot_model_failure_text(self):
        error_text = "All chat models failed: BadRequestError: invalid tool_calls"
        context = build_plain_chat_context(
            [{"role": "assistant", "content": error_text}]
        )

        self.assertEqual(context, [])
        self.assertTrue(is_llm_failure_response("err", "任意错误"))
        self.assertTrue(is_llm_failure_response("assistant", error_text))
        self.assertFalse(is_llm_failure_response("assistant", "正常回复"))


if __name__ == "__main__":
    unittest.main()
