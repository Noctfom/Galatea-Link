# 远程智能体决策测试，验证可见快照、合法动作和过期请求保护

import asyncio
import unittest

from core.link_events import LinkEventBus
from core.remote_decisions import RemoteDecisionBroker


class RemoteDecisionBrokerTests(unittest.IsolatedAsyncioTestCase):
    # 验证远程智能体可以提交当前合法动作并唤醒等待任务
    async def test_accepts_current_legal_choice(self):
        events = LinkEventBus()
        broker = RemoteDecisionBroker(events)
        observation = {
            "observation_id": 7,
            "legal_actions": [{"choice_id": 0}, {"choice_id": 2}],
        }
        task = asyncio.create_task(broker.decide(4, observation, 1.0))
        await asyncio.sleep(0)
        pending = broker.get_pending()
        result = broker.submit(
            4,
            {
                "session_id": pending["session_id"],
                "observation_id": 7,
                "choice_id": 2,
                "reason": "选择合法动作",
            },
        )
        decision = await task
        self.assertTrue(result["accepted"])
        self.assertEqual(decision.choice_id, 2)
        self.assertEqual(decision.reason, "选择合法动作")
        self.assertIsNone(broker.get_pending())
        events.close()

    # 验证远程智能体不能提交非法动作或其他会话请求
    async def test_rejects_invalid_or_foreign_submission(self):
        events = LinkEventBus()
        broker = RemoteDecisionBroker(events)
        task = asyncio.create_task(
            broker.decide(
                5,
                {
                    "observation_id": 8,
                    "legal_actions": [{"choice_id": 1}],
                },
                0.1,
            )
        )
        await asyncio.sleep(0)
        pending = broker.get_pending()
        with self.assertRaisesRegex(RuntimeError, "会话不匹配"):
            broker.submit(
                5,
                {
                    "session_id": "foreign",
                    "observation_id": 8,
                    "choice_id": 1,
                },
            )
        with self.assertRaisesRegex(ValueError, "合法 choice_id"):
            broker.submit(
                5,
                {
                    "session_id": pending["session_id"],
                    "observation_id": 8,
                    "choice_id": 9,
                },
            )
        with self.assertRaises(asyncio.TimeoutError):
            await task
        events.close()


if __name__ == "__main__":
    unittest.main()
