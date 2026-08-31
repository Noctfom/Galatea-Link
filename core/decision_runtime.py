# 决策运行时模块，负责异步执行、过期拦截和单次提交

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class DecisionRequest:
    request_id: int
    msg_type: int
    raw_msg: bytes
    snapshot: Any
    ignore_actions: tuple[Any, ...] = ()
    observation: Any = None


DecisionWorker = Callable[[DecisionRequest], Awaitable[Any]]
DecisionCommitter = Callable[[DecisionRequest, Any], Awaitable[None]]
DecisionErrorHandler = Callable[[DecisionRequest, Exception], Awaitable[None]]


class DecisionCoordinator:
    # 初始化决策工作器、提交器和异常处理器
    def __init__(
        self,
        worker: DecisionWorker,
        committer: DecisionCommitter,
        error_handler: DecisionErrorHandler | None = None,
    ):
        self._worker = worker
        self._committer = committer
        self._error_handler = error_handler
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._active_request_id: int | None = None
        self._active_task: asyncio.Task | None = None
        self._closed = False

    # 提交新的决策请求并让旧请求立即失效
    async def submit(
        self,
        msg_type: int,
        raw_msg: bytes,
        snapshot: Any,
        ignore_actions: tuple[Any, ...] = (),
        observation: Any = None,
    ) -> DecisionRequest:
        async with self._lock:
            if self._closed:
                raise RuntimeError("决策协调器已经关闭")

            previous_task = self._active_task
            self._sequence += 1
            request = DecisionRequest(
                request_id=self._sequence,
                msg_type=msg_type,
                raw_msg=raw_msg,
                snapshot=snapshot,
                ignore_actions=ignore_actions,
                observation=observation,
            )
            self._active_request_id = request.request_id
            self._active_task = asyncio.create_task(self._execute(request))

        if previous_task is not None and not previous_task.done():
            previous_task.cancel()
        return request

    # 执行策略计算并仅提交仍然有效的结果
    async def _execute(self, request: DecisionRequest) -> None:
        try:
            result = await self._worker(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._error_handler is not None:
                await self._error_handler(request, exc)
            return

        async with self._lock:
            if self._closed or self._active_request_id != request.request_id:
                return
            await self._committer(request, result)
            if self._active_request_id == request.request_id:
                self._active_request_id = None
                self._active_task = None

    # 取消当前决策并让尚未完成的计算结果失效
    async def cancel_active(self) -> None:
        async with self._lock:
            task = self._active_task
            self._active_request_id = None
            self._active_task = None

        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # 等待当前决策任务完成以便测试和安全关闭
    async def wait_idle(self) -> None:
        task = self._active_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    # 关闭协调器并取消仍在运行的决策
    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            task = self._active_task
            self._active_request_id = None
            self._active_task = None

        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
