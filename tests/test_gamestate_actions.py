# 对局动作语义测试，验证协议选项描述编号不会在快照前丢失

import struct
import unittest

from core.gamestate import DuelState


class GameStateActionTests(unittest.TestCase):
    # 验证 SELECT_OPTION 保存每个选项的 description_id
    def test_select_option_preserves_description_ids(self):
        state = DuelState()
        payload = bytes([0, 2]) + struct.pack("<II", 123456, 654321)

        state.update(14, payload)

        self.assertEqual(len(state.current_valid_actions), 2)
        self.assertEqual(state.current_valid_actions[0].desc_id, 123456)
        self.assertEqual(state.current_valid_actions[1].desc_id, 654321)

    # 验证选择合计消息从模式字节之后读取 Core 玩家编号
    def test_select_sum_reads_player_after_mode_byte(self):
        state = DuelState()
        payload = bytes([0, 1]) + bytes(6) + bytes([0, 0])

        state.update(23, payload)

        self.assertEqual(state.active_player, 1)


if __name__ == "__main__":
    unittest.main()
