# 远程智能体决策通道，保存时点快照并校验会话身份、期限和单次动作提交

import asyncio
import copy
import time
import uuid
from typing import Any, Mapping

from agents.llm_client import LlmDecision, OpenAICompatibleLlmClient


class RemoteDecisionBroker:
    # 为每次 Link 实例创建不可复用的远程会话标识
    def __init__(self, event_bus):
        self.session_id = str(uuid.uuid4())
        self._events = event_bus
        self._pending = None
        self._future = None
        self._deadline = 0.0

    # 返回尚未完成的请求快照以支持事件断线后的主动恢复
    def get_pending(self) -> dict[str, Any] | None:
        if self._future is None or self._future.done() or time.monotonic() >= self._deadline:
            return None
        return copy.deepcopy(self._pending)

    # 发布固定时点观察并等待远程智能体在预算内返回动作
    async def decide(self, request_id: int, observation: dict, budget: float) -> LlmDecision:
        if self._future is not None:
            raise RuntimeError("已有远程决策请求尚未结束")
        future = asyncio.get_running_loop().create_future()
        self._future = future
        self._deadline = time.monotonic() + budget
        self._pending = {
            "schema_version": "galatea.remote_decision.v1",
            "session_id": self.session_id,
            "request_id": request_id,
            "observation_id": observation.get("observation_id"),
            "expires_at": time.time() + budget,
            "budget_seconds": budget,
            "observation": copy.deepcopy(observation),
        }
        self._events.publish("agent.decision.requested", self._pending)
        try:
            return await asyncio.wait_for(future, timeout=budget)
        finally:
            self._pending = None
            self._future = None
            self._events.publish(
                "agent.decision.closed",
                {"session_id": self.session_id, "request_id": request_id},
            )

    # 验证远程动作属于当前时点并只接受一次结果
    def submit(self, request_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        pending = self.get_pending()
        if pending is None or request_id != pending["request_id"]:
            raise RuntimeError("远程决策请求已结束或已过期")
        if payload.get("session_id") != self.session_id:
            raise RuntimeError("远程决策会话不匹配")
        if payload.get("observation_id") != pending["observation_id"]:
            raise RuntimeError("远程决策观察版本不匹配")
        choice_id = payload.get("choice_id")
        allowed = {
            item["choice_id"]
            for item in pending["observation"].get("legal_actions", [])
        }
        if isinstance(choice_id, bool) or not isinstance(choice_id, int) or choice_id not in allowed:
            raise ValueError("远程智能体必须选择当前合法 choice_id")
        reason = payload.get("reason", "")
        chat_message = payload.get("chat_message")
        if not isinstance(reason, str) or len(reason) > 4000:
            raise ValueError("决策理由必须是不超过 4000 字符的文本")
        if chat_message is not None and (not isinstance(chat_message, str) or len(chat_message) > 2000):
            raise ValueError("决策聊天必须是不超过 2000 字符的文本或 null")
        update = OpenAICompatibleLlmClient._parse_intervention_update(
            payload.get("intervention_update")
        )
        self._future.set_result(LlmDecision(choice_id, reason, chat_message, update))
        return {"accepted": True, "session_id": self.session_id, "request_id": request_id}
