# dsh 端到端测试服务，使用真实 Link API 与决策通道模拟一局可控对局

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from app_config import ServiceConfig
from core.link_events import LinkEventBus
from core.remote_decisions import RemoteDecisionBroker
from service.http_api import create_http_app
from service.session_manager import LinkSessionManager


class FixtureLink:
    # 创建不含模型与游戏网络的真实远程决策运行时
    def __init__(self):
        self.events = LinkEventBus()
        self.broker = RemoteDecisionBroker(self.events)
        self.runtime = self
        self.finished = asyncio.Event()

    # 提供真实 Link 事件订阅
    def subscribe_events(self, max_queue_size=256):
        return self.events.subscribe(max_queue_size)

    # 返回测试对局状态
    def get_status(self):
        return {"connected": True, "duel_active": True}

    # 返回真实待处理决策
    def get_pending_decision(self):
        return self.broker.get_pending()

    # 使用真实 Broker 校验动作提交
    def submit_remote_decision(self, request_id, payload):
        return self.broker.submit(request_id, payload)

    # 接收游戏发言并发布可检查的发言事件
    async def send_game_chat(self, text, **kwargs):
        self.events.publish("fixture.chat.sent", {"text": text})
        return {"sent": True}

    # 模拟决策后对手发言并等待停止
    async def start(self):
        await asyncio.sleep(0.15)
        result = await self.broker.decide(1, {"observation_id": 1, "legal_actions": [{"choice_id": 0}, {"choice_id": 2}]}, 5, backend="remote_dsh")
        self.events.publish("decision.completed", {"choice_id": result.choice_id, "source": "dsh"})
        await asyncio.sleep(0.1)
        self.events.publish("game_chat.received", {"sequence": 1, "role": "opponent", "text": "你好"})
        await asyncio.sleep(0.3)
        self.events.publish("duel.result", {"outcome": "win", "reason": "测试结束"})
        await self.finished.wait()

    # 清理测试事件源并终止模拟对局
    async def close(self):
        self.finished.set()
        self.events.close()


# 在临时目录与随机本机端口上提供端到端测试 API
async def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config_path = root / "config.yaml"
        config_path.write_text("decision:\n  mode: hybrid\nagent:\n  deck: test\n", encoding="utf-8")
        manager = LinkSessionManager(config_path, runtime_builder=lambda path: FixtureLink())
        manager._decks.import_local_ydk("test.ydk", "#main\n89631139\n#extra\n23995346\n!side\n")
        runner = web.AppRunner(create_http_app(manager, ServiceConfig(), api_token="fixture-token"))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        print(f"DSH_FIXTURE_URL=http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
