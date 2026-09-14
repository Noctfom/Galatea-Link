# 决策运行时测试，验证后台执行、过期拦截和单次提交

import asyncio
import unittest

from core.decision_runtime import DecisionCoordinator


class DecisionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    # 验证提交请求不会等待后台决策完成
    async def test_submit_returns_before_worker_finishes(self):
        worker_started = asyncio.Event()
        release_worker = asyncio.Event()
        commits = []

        async def worker(request):
            # 等待测试释放以模拟耗时模型调用
            worker_started.set()
            await release_worker.wait()
            return request.msg_type

        async def committer(request, result):
            # 记录最终提交结果以验证只提交一次
            commits.append((request.request_id, result))

        coordinator = DecisionCoordinator(worker, committer)
        request = await coordinator.submit(16, b"\x10", object())
        await worker_started.wait()

        self.assertEqual(request.request_id, 1)
        self.assertEqual(commits, [])

        release_worker.set()
        await coordinator.wait_idle()
        self.assertEqual(commits, [(1, 16)])
        await coordinator.close()

    # 验证新请求会取消旧请求并阻止旧结果提交
    async def test_new_request_invalidates_previous_result(self):
        first_started = asyncio.Event()
        keep_first_running = asyncio.Event()
        commits = []

        async def worker(request):
            # 让首个请求保持运行直到被新请求取消
            if request.msg_type == 1:
                first_started.set()
                await keep_first_running.wait()
            return request.msg_type

        async def committer(request, result):
            # 保存实际通过仲裁的请求编号和结果
            commits.append((request.request_id, result))

        coordinator = DecisionCoordinator(worker, committer)
        await coordinator.submit(1, b"old", object())
        await first_started.wait()
        replacement = await coordinator.submit(2, b"new", object())
        await coordinator.wait_idle()

        self.assertEqual(replacement.request_id, 2)
        self.assertEqual(commits, [(2, 2)])
        await coordinator.close()

    # 验证取消当前请求后不会提交任何动作
    async def test_cancel_active_discards_result(self):
        worker_started = asyncio.Event()
        release_worker = asyncio.Event()
        commits = []

        async def worker(request):
            # 模拟可被取消的远程决策调用
            worker_started.set()
            await release_worker.wait()
            return request.msg_type

        async def committer(request, result):
            # 记录不应出现的提交结果
            commits.append(result)

        coordinator = DecisionCoordinator(worker, committer)
        await coordinator.submit(13, b"cancel", object())
        await worker_started.wait()
        await coordinator.cancel_active()

        self.assertEqual(commits, [])
        await coordinator.close()

    # 验证慢速决策运行时不会阻塞同一事件循环的其他任务
    async def test_slow_worker_keeps_event_loop_responsive(self):
        release_worker = asyncio.Event()
        heartbeat_processed = asyncio.Event()

        async def worker(request):
            # 模拟等待远程 LLM 返回的慢速决策
            await release_worker.wait()
            return request.msg_type

        async def committer(request, result):
            # 当前测试只关心事件循环可响应性
            return None

        async def heartbeat():
            # 模拟决策期间到达的网络心跳处理
            await asyncio.sleep(0)
            heartbeat_processed.set()

        coordinator = DecisionCoordinator(worker, committer)
        await coordinator.submit(16, b"slow", object())
        heartbeat_task = asyncio.create_task(heartbeat())
        await asyncio.wait_for(heartbeat_processed.wait(), timeout=0.1)

        self.assertTrue(heartbeat_processed.is_set())
        release_worker.set()
        await coordinator.wait_idle()
        await heartbeat_task
        await coordinator.close()

    # 验证提交阶段异常也会被报告并清理活动请求
    async def test_committer_failure_reports_error_and_clears_request(self):
        errors = []

        async def worker(request):
            # 返回可进入提交阶段的固定结果
            return request.msg_type

        async def committer(request, result):
            # 模拟网络提交阶段失败
            raise RuntimeError("提交失败")

        async def error_handler(request, error):
            # 保存异常与请求编号供断言
            errors.append((request.request_id, str(error)))

        coordinator = DecisionCoordinator(worker, committer, error_handler)
        await coordinator.submit(13, b"commit", object())
        await coordinator.wait_idle()

        self.assertEqual(errors, [(1, "提交失败")])
        self.assertIsNone(coordinator.active_request_id)
        await coordinator.close()


if __name__ == "__main__":
    unittest.main()
