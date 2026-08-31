# LLM API 客户端模块，负责异步请求兼容接口并校验结构化动作选择

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app_config import LlmConfig


DEFAULT_SYSTEM_PROMPT = """你是正在进行游戏王对局的决策智能体
你只能使用用户提供的可见观察和合法动作，不得推测隐藏卡片身份
从 legal_actions 中选择一个 choice_id
只输出 JSON 对象，字段为 choice_id、reason、chat_message
reason 使用简短中文说明，chat_message 不需要发送时必须为 null
不得输出观察中不存在的动作编号
如果 information_quality 标记某类信息不完整，必须将其视为未知而不是自行补全
"""

DECISION_JSON_SCHEMA = {
    "name": "galatea_duel_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "choice_id": {"type": "integer"},
            "reason": {"type": "string"},
            "chat_message": {"type": ["string", "null"]},
        },
        "required": ["choice_id", "reason", "chat_message"],
        "additionalProperties": False,
    },
}


class LlmClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmDecision:
    choice_id: int
    reason: str
    chat_message: str | None = None


class OpenAICompatibleLlmClient:
    # 初始化兼容 Chat Completions 的异步客户端
    def __init__(
        self,
        config: LlmConfig,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.config = config
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=config.timeout)

    # 请求模型选择一个合法动作并返回校验后的结果
    async def decide(
        self,
        observation: dict[str, Any],
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> LlmDecision:
        legal_actions = observation.get("legal_actions", [])
        valid_choice_ids = {
            action.get("choice_id")
            for action in legal_actions
            if isinstance(action.get("choice_id"), int)
        }
        if not observation.get("decision_required") or not valid_choice_ids:
            raise LlmClientError("当前观察不需要 LLM 决策")
        if not self.config.model:
            raise LlmClientError("尚未配置 llm.model")

        try:
            response = await self._http_client.post(
                self._completion_url(),
                headers=self._headers(),
                json=self._build_payload(observation, system_prompt),
                timeout=self.config.timeout,
            )
        except httpx.TimeoutException as exc:
            raise LlmClientError(
                f"LLM API 请求超时，限制为 {self.config.timeout:.1f} 秒"
            ) from exc
        except httpx.RequestError as exc:
            raise LlmClientError(f"LLM API 网络请求失败: {exc}") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = self._response_preview(response.text, limit=500)
            raise LlmClientError(f"LLM API 返回 HTTP {response.status_code}: {body}") from exc

        try:
            response_data = response.json()
            choice = response_data["choices"][0]
            message = choice["message"]
            content = message["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            preview = self._response_preview(response.text)
            raise LlmClientError(
                f"LLM API 响应缺少 choices[0].message.content，响应预览: {preview}"
            ) from exc

        diagnostics = {
            "finish_reason": choice.get("finish_reason"),
            "content": content,
        }
        if message.get("reasoning_content"):
            diagnostics["reasoning_content"] = message["reasoning_content"]
        return self._parse_decision(content, valid_choice_ids, diagnostics)

    # 关闭当前客户端创建的网络连接池
    async def close(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    # 构建兼容 Chat Completions 的请求体
    def _build_payload(
        self,
        observation: dict[str, Any],
        system_prompt: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(observation, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif self.config.response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": DECISION_JSON_SCHEMA,
            }
        return payload

    # 解析模型 JSON 并拒绝不存在的动作编号
    def _parse_decision(
        self,
        content: Any,
        valid_choice_ids: set[int],
        diagnostics: dict[str, Any] | None = None,
    ) -> LlmDecision:
        if not isinstance(content, str):
            preview = self._response_preview(diagnostics or content)
            raise LlmClientError(f"LLM 决策内容必须是字符串，内容预览: {preview}")
        normalized = self._strip_reasoning_blocks(content.strip())
        normalized = self._strip_code_fence(normalized)
        payload = self._extract_json_payload(normalized)
        if payload is None:
            preview = self._response_preview(diagnostics or content)
            raise LlmClientError(f"LLM 未返回有效 JSON，原始内容预览: {preview}")
        if not isinstance(payload, dict):
            preview = self._response_preview(diagnostics or content)
            raise LlmClientError(f"LLM 决策必须是 JSON 对象，原始内容预览: {preview}")

        choice_id = payload.get("choice_id")
        if isinstance(choice_id, bool) or not isinstance(choice_id, int):
            raise LlmClientError("LLM 返回的 choice_id 必须是整数")
        if choice_id not in valid_choice_ids:
            raise LlmClientError(f"LLM 返回了非法 choice_id: {choice_id}")

        reason = payload.get("reason", "")
        chat_message = payload.get("chat_message")
        if not isinstance(reason, str):
            raise LlmClientError("LLM 返回的 reason 必须是字符串")
        if chat_message is not None and not isinstance(chat_message, str):
            raise LlmClientError("LLM 返回的 chat_message 必须是字符串或 null")
        return LlmDecision(
            choice_id=choice_id,
            reason=reason.strip(),
            chat_message=chat_message.strip() if chat_message else None,
        )

    # 移除常见 Markdown JSON 代码块包装
    def _strip_code_fence(self, content: str) -> str:
        if not content.startswith("```"):
            return content
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines.pop(0)
        if lines and lines[-1].strip() == "```":
            lines.pop()
        return "\n".join(lines).strip()

    # 移除常见推理标签以避免把思考文本误当成最终答案
    def _strip_reasoning_blocks(self, content: str) -> str:
        return re.sub(
            r"<(think|analysis|reasoning)>.*?</\1>",
            "",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()

    # 从纯 JSON 或前后带说明的文本中提取最终决策对象
    def _extract_json_payload(self, content: str) -> Any | None:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        decoder = json.JSONDecoder()
        candidates = []
        for index, character in enumerate(content):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(content[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "choice_id" in candidate:
                candidates.append(candidate)
        return candidates[-1] if candidates else None

    # 生成长度受限且保留转义符的诊断预览
    def _response_preview(self, content: Any, limit: int = 1200) -> str:
        text = content if isinstance(content, str) else repr(content)
        if len(text) > limit:
            text = f"{text[:limit]}...<已截断 {len(text) - limit} 字符>"
        return repr(text)

    # 生成兼容服务端的请求地址
    def _completion_url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    # 生成请求头并优先从环境变量读取密钥
    def _headers(self) -> dict[str, str]:
        api_key = ""
        if self.config.api_key_env:
            api_key = os.getenv(self.config.api_key_env, "")
        if not api_key:
            api_key = self.config.api_key

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers


# 根据配置创建已启用的 LLM 客户端
def create_llm_client(config: LlmConfig) -> OpenAICompatibleLlmClient | None:
    if not config.enabled:
        return None
    if config.provider != "openai_compatible":
        raise ValueError(f"暂不支持 LLM provider: {config.provider}")
    return OpenAICompatibleLlmClient(config)
