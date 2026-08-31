# LLM API 客户端模块，负责异步请求兼容接口并校验结构化动作选择

import copy
import hashlib
import json
import os
import re
import sys
import time
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
即使信息不足也必须从 legal_actions 选择风险最低的动作，不得拒绝、取消或要求补充信息
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


class LlmDecisionSkipped(LlmClientError):
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
        self._static_context_text = ""
        self._static_context_key = "none"
        self._static_card_codes: set[int] = set()
        self._static_card_text_codes: set[int] = set()

    # 设置本局可重复命中的固定提示前缀
    def set_static_context(self, context: dict[str, Any]) -> None:
        if not self.config.cache_static_context:
            return
        self._static_context_text = json.dumps(
            context,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._static_context_key = hashlib.sha256(
            self._static_context_text.encode("utf-8")
        ).hexdigest()[:16]
        self._static_card_codes = set()
        self._static_card_text_codes = set()
        deck = context.get("own_initial_deck", {})
        for section in ("main", "extra"):
            for card in deck.get(section, []):
                code = card.get("code")
                if not isinstance(code, int):
                    continue
                self._static_card_codes.add(code)
                if card.get("text"):
                    self._static_card_text_codes.add(code)

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
            raise LlmDecisionSkipped("当前观察没有可提交的合法动作")
        if not self.config.model:
            raise LlmClientError("尚未配置 llm.model")

        request_payload = self._build_payload(observation, system_prompt)
        user_content = request_payload["messages"][-1]["content"]
        canonical_content = json.dumps(
            observation,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        started_at = time.perf_counter()
        self._trace(
            "💬 [LLM 请求] "
            f"model={self.config.model} "
            f"actions={len(valid_choice_ids)} "
            f"dynamic_chars={len(user_content)} "
            f"static_chars={len(self._static_context_text)} "
            f"saved_chars={len(canonical_content) - len(user_content)} "
            f"cache_key={self._static_context_key} "
            f"thinking={self.config.thinking_mode} "
            f"timeout={self.config.timeout:.1f}s"
        )
        try:
            response = await self._http_client.post(
                self._completion_url(),
                headers=self._headers(),
                json=request_payload,
                timeout=self.config.timeout,
            )
        except httpx.TimeoutException as exc:
            elapsed = time.perf_counter() - started_at
            raise LlmClientError(
                f"LLM API 请求超时，耗时 {elapsed:.2f} 秒，"
                f"限制为 {self.config.timeout:.1f} 秒，动态输入 {len(user_content)} 字符"
            ) from exc
        except httpx.RequestError as exc:
            elapsed = time.perf_counter() - started_at
            raise LlmClientError(
                f"LLM API 网络请求失败，耗时 {elapsed:.2f} 秒: {exc}"
            ) from exc
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
        usage = response_data.get("usage") or {}
        usage_metrics = self._extract_usage_metrics(usage)
        elapsed = time.perf_counter() - started_at
        self._trace(
            "💬 [LLM 响应] "
            f"elapsed={elapsed:.2f}s "
            f"finish_reason={choice.get('finish_reason')} "
            f"prompt_tokens={usage_metrics['prompt_tokens']} "
            f"completion_tokens={usage_metrics['completion_tokens']} "
            f"reasoning_tokens={usage_metrics['reasoning_tokens']} "
            f"cache_hit_tokens={usage_metrics['cache_hit_tokens']} "
            f"cache_miss_tokens={usage_metrics['cache_miss_tokens']} "
            f"cache_hit_rate={usage_metrics['cache_hit_rate']} "
            f"content_chars={len(content) if isinstance(content, str) else 'unknown'}"
        )
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
        compact_observation = self._compact_observation(observation)
        messages = [{"role": "system", "content": system_prompt}]
        if self._static_context_text:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "以下是本局固定上下文 后续动态观察会引用其中的卡密\n"
                        f"{self._static_context_text}"
                    ),
                }
            )
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    compact_observation,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.thinking_mode != "auto":
            payload["thinking"] = {"type": self.config.thinking_mode}
        if self.config.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif self.config.response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": DECISION_JSON_SCHEMA,
            }
        return payload

    # 移除固定前缀中已经完整提供的重复动态卡片信息
    def _compact_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        if (
            not self.config.compact_dynamic_observation
            or not self._static_context_text
        ):
            return observation

        compacted = copy.deepcopy(observation)
        card_catalog = compacted.get("card_catalog")
        if isinstance(card_catalog, list):
            compacted["card_catalog"] = [
                card
                for card in card_catalog
                if card.get("code") not in self._static_card_text_codes
            ]

        known_information = compacted.get("known_information")
        if isinstance(known_information, dict):
            for section in ("own_remaining_deck", "own_remaining_extra"):
                cards = known_information.get(section)
                if not isinstance(cards, list):
                    continue
                for card in cards:
                    if card.get("code") in self._static_card_codes:
                        card.pop("name", None)
                        card.pop("text", None)
        return compacted

    # 兼容提取不同服务端返回的 token 与缓存统计
    def _extract_usage_metrics(self, usage: dict[str, Any]) -> dict[str, Any]:
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get(
            "completion_tokens",
            usage.get("output_tokens"),
        )
        cache_hit_tokens = usage.get("prompt_cache_hit_tokens")
        cache_miss_tokens = usage.get("prompt_cache_miss_tokens")

        if cache_hit_tokens is None:
            for detail_key in ("prompt_tokens_details", "input_tokens_details"):
                details = usage.get(detail_key)
                if isinstance(details, dict) and "cached_tokens" in details:
                    cache_hit_tokens = details["cached_tokens"]
                    break
        if cache_hit_tokens is None:
            cache_hit_tokens = usage.get("cache_read_input_tokens")
        if (
            cache_miss_tokens is None
            and isinstance(prompt_tokens, int)
            and isinstance(cache_hit_tokens, int)
        ):
            cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)

        completion_details = usage.get("completion_tokens_details")
        reasoning_tokens = None
        if isinstance(completion_details, dict):
            reasoning_tokens = completion_details.get("reasoning_tokens")
        if reasoning_tokens is None:
            output_details = usage.get("output_tokens_details")
            if isinstance(output_details, dict):
                reasoning_tokens = output_details.get("reasoning_tokens")

        cache_hit_rate = "unknown"
        if (
            isinstance(prompt_tokens, int)
            and prompt_tokens > 0
            and isinstance(cache_hit_tokens, int)
        ):
            cache_hit_rate = f"{cache_hit_tokens / prompt_tokens:.1%}"
        return {
            "prompt_tokens": prompt_tokens if prompt_tokens is not None else "unknown",
            "completion_tokens": (
                completion_tokens if completion_tokens is not None else "unknown"
            ),
            "reasoning_tokens": (
                reasoning_tokens if reasoning_tokens is not None else "unknown"
            ),
            "cache_hit_tokens": (
                cache_hit_tokens if cache_hit_tokens is not None else "unknown"
            ),
            "cache_miss_tokens": (
                cache_miss_tokens if cache_miss_tokens is not None else "unknown"
            ),
            "cache_hit_rate": cache_hit_rate,
        }

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

    # 按配置输出不包含密钥和请求正文的诊断日志
    def _trace(self, message: str) -> None:
        if not self.config.trace_requests:
            return
        try:
            print(message)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe_message = message.encode(encoding, errors="replace").decode(encoding)
            print(safe_message)

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
