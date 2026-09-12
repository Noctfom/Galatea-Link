# 卡片关系测试，验证装备、取对象和离场清理会进入 LLM 观察

import struct
import unittest

from core.gamestate import DuelState
from core.observation import LlmObservationBuilder
from game_constants import LocationInfo, Position, Zone


class FakeCardReader:
    # 返回便于断言的测试卡名
    def get_card_name(self, code):
        return f"Card {code}"

    # 返回便于断言的测试卡片文本
    def get_card_text(self, code):
        return f"Text {code}"

    # 当前测试不需要效果选项文本
    def get_effect_description(self, description_id, card_code=0):
        return ""


# 创建字段映射使用的卡片状态
def make_card_info(code, owner, position=Position.FACEUP_ATTACK):
    return {
        "code": code,
        "pos": position,
        "owner": owner,
        "counters": 0,
        "overlays": [],
        "is_equipped": False,
    }


class CardRelationTests(unittest.TestCase):
    # 验证装备和取对象关系会转换成稳定实体编号
    def test_relations_are_exposed_in_observation(self):
        state = DuelState()
        state.field_map[0][Zone.MZONE][0] = make_card_info(10001, 0)
        state.field_map[0][Zone.SZONE][0] = make_card_info(10002, 0)
        state.field_map[1][Zone.MZONE][0] = make_card_info(10003, 1)
        equip = LocationInfo.encode(0, Zone.SZONE, 0)
        own_monster = LocationInfo.encode(0, Zone.MZONE, 0)
        opponent_monster = LocationInfo.encode(1, Zone.MZONE, 0)

        state.update(93, struct.pack("<II", equip, own_monster))
        state.update(96, struct.pack("<II", own_monster, opponent_monster))
        snapshot = state.get_snapshot()
        observation = LlmObservationBuilder(FakeCardReader()).build(
            snapshot,
            player_id=0,
            event_type=96,
        )

        cards = {card["entity_id"]: card for card in observation["cards"]}
        self.assertEqual(
            cards["self:8:0"]["equip_target_entity_id"],
            "self:4:0",
        )
        self.assertEqual(
            cards["self:4:0"]["equipped_by_entity_ids"],
            ["self:8:0"],
        )
        self.assertEqual(
            cards["self:4:0"]["target_entity_ids"],
            ["opponent:4:0"],
        )
        self.assertEqual(
            cards["opponent:4:0"]["targeted_by_entity_ids"],
            ["self:4:0"],
        )

    # 验证取消取对象和卡片离场会清除旧关系
    def test_relations_are_removed_when_cancelled_or_moved(self):
        state = DuelState()
        state.field_map[0][Zone.MZONE][0] = make_card_info(10001, 0)
        state.field_map[1][Zone.MZONE][0] = make_card_info(10003, 1)
        source = LocationInfo.encode(0, Zone.MZONE, 0)
        target = LocationInfo.encode(1, Zone.MZONE, 0)

        state.update(96, struct.pack("<II", source, target))
        state.update(97, struct.pack("<II", source, target))

        self.assertEqual(state.field_map[0][Zone.MZONE][0].get("targets"), [])
        self.assertEqual(state.field_map[1][Zone.MZONE][0].get("targeted_by"), [])


if __name__ == "__main__":
    unittest.main()
