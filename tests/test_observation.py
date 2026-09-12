# LLM 可见观察测试，验证视角换算、隐藏信息隔离和合法动作输出

import json
import unittest

from core.observation import LlmObservationBuilder
from data_types import CardEntity, GameAction, GameSnapshot, GlobalFeature
from game_constants import Position, Zone


class FakeCardReader:
    # 返回便于断言的测试卡名
    def get_card_name(self, code):
        return f"Card {code}"

    # 返回便于断言的测试效果文本
    def get_card_text(self, code):
        return f"Text {code}"

    # 返回便于断言的测试效果选项文本
    def get_effect_description(self, description_id, card_code=0):
        return f"Effect {card_code}:{description_id}"


# 创建测试所需的全局状态
def make_global(to_play=0):
    return GlobalFeature(
        turn_count=2,
        phase_id=4,
        to_play=to_play,
        my_lp=7000,
        op_lp=6500,
        my_hand_len=2,
        op_hand_len=3,
        my_deck_len=30,
        op_deck_len=28,
        my_grave_len=4,
        op_grave_len=5,
        my_removed_len=1,
        op_removed_len=2,
        my_extra_len=12,
        op_extra_len=11,
    )


# 创建指定身份和区域的测试卡片
def make_entity(code, owner, location, sequence, position, is_public):
    return CardEntity(
        code=code,
        owner=owner,
        location=location,
        sequence=sequence,
        position=position,
        current_atk=2500,
        current_def=2000,
        type_mask=1,
        race=1,
        attribute=1,
        level=8,
        base_atk=2500,
        base_def=2000,
        is_public=is_public,
    )


class LlmObservationTests(unittest.TestCase):
    # 验证对手隐藏卡密和卡组内容不会进入序列化结果
    def test_hidden_opponent_information_is_removed(self):
        hidden_opponent = make_entity(22222222, 1, Zone.HAND, 0, 0, False)
        hidden_opponent.overlay_codes = (88888888,)
        public_opponent = make_entity(
            33333333,
            1,
            Zone.MZONE,
            0,
            Position.FACEUP_ATTACK,
            True,
        )
        public_opponent.overlay_codes = (77777777,)
        public_opponent.overlay_count = 1
        public_opponent.used_effect_mask = 4
        snapshot = GameSnapshot(
            global_data=make_global(),
            entities=[
                make_entity(11111111, 0, Zone.HAND, 0, 0, False),
                hidden_opponent,
                public_opponent,
            ],
            p0_deck_codes=[44444444],
            p1_deck_codes=[55555555],
        )
        snapshot.known_hand_codes = {0: [], 1: [66666666]}
        builder = LlmObservationBuilder(FakeCardReader())

        observation = builder.build(snapshot, player_id=0, event_type=50)
        serialized = json.dumps(observation)

        self.assertNotIn("22222222", serialized)
        self.assertNotIn("55555555", serialized)
        self.assertIn("33333333", serialized)
        self.assertIn("44444444", serialized)
        self.assertIn("66666666", serialized)
        self.assertIn("77777777", serialized)
        self.assertNotIn("88888888", serialized)
        self.assertEqual(
            next(
                card
                for card in observation["cards"]
                if card.get("code") == 33333333
            )["used_effect_mask"],
            4,
        )
        self.assertFalse(
            observation["information_quality"]["complete_protocol_projection"]
        )
        hidden = next(card for card in observation["cards"] if card["visibility"] == "hidden")
        self.assertNotIn("code", hidden)
        self.assertNotIn("current_atk", hidden)

    # 验证只有本方行动时才向 LLM 暴露合法动作
    def test_legal_actions_only_appear_for_self(self):
        own_card = make_entity(11111111, 0, Zone.MZONE, 0, Position.FACEUP_ATTACK, True)
        action = GameAction(
            action_type=5,
            index=0,
            target_entity_idx=0,
            desc_str="Activate",
            desc_id=123,
        )
        snapshot = GameSnapshot(
            global_data=make_global(to_play=0),
            entities=[own_card],
            valid_actions=[action],
        )
        builder = LlmObservationBuilder(FakeCardReader())

        own_view = builder.build(snapshot, player_id=0, event_type=11)
        opponent_view = builder.build(snapshot, player_id=1, event_type=11)

        self.assertTrue(own_view["decision_required"])
        self.assertEqual(own_view["legal_actions"][0]["choice_id"], 0)
        self.assertEqual(own_view["legal_actions"][0]["target_entity_id"], "self:4:0")
        self.assertEqual(
            own_view["legal_actions"][0]["effect_description"],
            "Effect 11111111:123",
        )
        self.assertFalse(opponent_view["decision_required"])
        self.assertEqual(opponent_view["legal_actions"], [])

    # 验证二号玩家视角会正确交换双方资源和己方卡组
    def test_player_one_perspective_swaps_resources(self):
        snapshot = GameSnapshot(
            global_data=make_global(to_play=1),
            entities=[],
            p0_deck_codes=[11111111],
            p1_deck_codes=[22222222, 22222222],
        )
        builder = LlmObservationBuilder(FakeCardReader(), include_card_text=False)

        observation = builder.build(snapshot, player_id=1, event_type=41)

        self.assertEqual(observation["players"]["self"]["lp"], 6500)
        self.assertEqual(observation["players"]["opponent"]["lp"], 7000)
        self.assertEqual(
            observation["known_information"]["own_remaining_deck"],
            [{"code": 22222222, "name": "Card 22222222", "count": 2}],
        )


if __name__ == "__main__":
    unittest.main()
