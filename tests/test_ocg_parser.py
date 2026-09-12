# 游戏消息解析测试，验证合并消息包的顺序拆分

import unittest
import struct

from core.parser import OCGParser


class OCGParserTests(unittest.TestCase):
    # 验证多个定长游戏消息可以按顺序拆分
    def test_parses_multiple_fixed_messages(self):
        data = bytes([40, 0, 41, 0x04, 0x00])
        messages = OCGParser.robust_parse(data)

        self.assertEqual(messages, [(40, b"\x00"), (41, b"\x04\x00")])

    # 验证交互消息后仍可继续解析下一条消息
    def test_parses_prompt_followed_by_turn_message(self):
        data = bytes([13, 0, 1, 0, 0, 0, 40, 1])
        messages = OCGParser.robust_parse(data)

        self.assertEqual(messages[0], (13, bytes([0, 1, 0, 0, 0])))
        self.assertEqual(messages[1], (40, b"\x01"))

    # 验证 Link 的确认卡片消息不会吞掉 Core 本地幽灵字节
    def test_confirm_cards_uses_standard_network_layout(self):
        card_entry = struct.pack("<I", 12345678) + bytes([0, 2, 0])
        data = bytes([31, 0, 1]) + card_entry + bytes([40, 1])

        messages = OCGParser.robust_parse(data)

        self.assertEqual(messages[0], (31, bytes([0, 1]) + card_entry))
        self.assertEqual(messages[1], (40, b"\x01"))

    # 验证 Link 的连锁选项之间不会解析 Core 本地定界字节
    def test_select_chain_uses_standard_network_layout(self):
        option = bytes([1]) + struct.pack("<I", 12345678)
        option += bytes([4, 0, 1]) + struct.pack("<I", 9) + bytes([0])
        payload = bytes([0, 1, 0]) + bytes(8) + option
        data = bytes([16]) + payload + bytes([40, 1])

        messages = OCGParser.robust_parse(data)

        self.assertEqual(messages[0], (16, payload))
        self.assertEqual(messages[1], (40, b"\x01"))

    # 验证只有消息号的残缺交互包不会进入 GameState
    def test_drops_empty_select_counter_message(self):
        messages = OCGParser.robust_parse(bytes([22]))

        self.assertEqual(messages, [])

    # 验证声明长度超过实际内容的交互包不会伪装成完整消息
    def test_drops_truncated_select_counter_message(self):
        data = bytes([22, 0, 1, 0, 1, 0, 1])

        messages = OCGParser.robust_parse(data)

        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
