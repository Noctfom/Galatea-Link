# 对局事件总线测试，验证广播、拥塞丢弃和关闭唤醒行为

import unittest

from core.link_events import EventStreamClosed, LinkEventBus


class LinkEventBusTests(unittest.IsolatedAsyncioTestCase):
    # 验证多个订阅者会收到内容相同但相互独立的事件
    async def test_broadcasts_independent_event_snapshots(self):
        bus = LinkEventBus()
        first = bus.subscribe()
        second = bus.subscribe()

        published = bus.publish("observation.updated", {"nested": {"value": 1}})
        first_event = await first.get()
        second_event = await second.get()
        first_event.payload["nested"]["value"] = 9

        self.assertEqual(first_event.sequence, published.sequence)
        self.assertEqual(second_event.payload["nested"]["value"], 1)
        first.close()
        second.close()
        bus.close()

    # 验证慢消费者只丢弃最旧事件而不会阻塞发布者
    async def test_drops_oldest_event_when_queue_is_full(self):
        bus = LinkEventBus()
        subscription = bus.subscribe(max_queue_size=1)

        bus.publish("first")
        bus.publish("second")
        event = await subscription.get()

        self.assertEqual(event.event_type, "second")
        self.assertEqual(subscription.dropped_events, 1)
        bus.close()

    # 验证关闭总线会唤醒正在读取的消费者
    async def test_close_wakes_subscriber(self):
        bus = LinkEventBus()
        subscription = bus.subscribe()
        bus.close()

        with self.assertRaises(EventStreamClosed):
            await subscription.get()


if __name__ == "__main__":
    unittest.main()
