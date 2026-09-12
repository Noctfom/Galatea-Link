# 游戏内聊天在线测试脚本，负责人工验证 YGOPro 聊天收发和运行时事件

import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path


# 将项目根目录加入直接运行脚本时的模块搜索路径
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from app_config import load_app_config
from core.link_events import EventStreamClosed
from core.network import _console_print
from galatea_link import create_link_from_config


# 解析在线测试使用的配置文件路径
def parse_args():
    parser = argparse.ArgumentParser(description="在线验证 YGOPro 游戏聊天接口")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="应用配置文件路径",
    )
    return parser.parse_args()


# 在守护线程中读取终端输入并投递到异步队列
def read_console_lines(loop, queue):
    while True:
        line = sys.stdin.readline()
        if not line:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, "/quit")
            except RuntimeError:
                pass
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, line.rstrip("\r\n"))
        except RuntimeError:
            return


# 持续打印聊天相关事件供人工核对
async def consume_chat_events(link):
    subscription = link.runtime.subscribe_events(max_queue_size=128)
    try:
        while True:
            event = await subscription.get()
            if event.event_type.startswith("game_chat.") or event.event_type == "chat.suggested":
                _console_print(
                    "[聊天事件] "
                    + json.dumps(event.to_dict(), ensure_ascii=False)
                )
    except EventStreamClosed:
        return
    finally:
        subscription.close()


# 等待连接建立或连接任务提前结束
async def wait_until_connected(link, link_task):
    while not link.client.is_connected or link.ai_player_id is None:
        if link_task.done():
            await link_task
            raise RuntimeError("游戏连接未能建立")
        await asyncio.sleep(0.1)


# 处理终端聊天、历史查询和退出命令
async def run_console(link, link_task):
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()
    threading.Thread(
        target=read_console_lines,
        args=(loop, lines),
        name="galatea-chat-console",
        daemon=True,
    ).start()
    _console_print("输入聊天内容并回车发送，/history 查看历史，/quit 退出")

    while not link_task.done():
        line_task = asyncio.create_task(lines.get())
        done, _ = await asyncio.wait(
            {line_task, link_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if link_task in done:
            line_task.cancel()
            await asyncio.gather(line_task, return_exceptions=True)
            break
        line = line_task.result().strip()
        if not line:
            continue
        if line == "/quit":
            await link.close()
            break
        if line == "/history":
            _console_print(
                json.dumps(
                    link.runtime.get_game_chat_history(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            continue
        try:
            sent = await link.runtime.send_game_chat(
                line,
                source="online_test_console",
            )
            _console_print(f"[聊天已发送] sequence={sent['sequence']}")
        except Exception as error:
            _console_print(f"[聊天发送失败] {error}")


# 启动连接、事件消费和交互式聊天测试
async def main():
    args = parse_args()
    link = create_link_from_config(load_app_config(args.config))
    event_task = asyncio.create_task(consume_chat_events(link))
    link_task = asyncio.create_task(link.start())
    try:
        await wait_until_connected(link, link_task)
        await run_console(link, link_task)
        await link_task
    finally:
        await link.close()
        await asyncio.gather(event_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
