"""Any OpenAI-compatible chat endpoint: Groq, OpenRouter, a local Ollama. Lets the agent run
on a different free tier, or fully offline, by changing three environment variables."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from app.llm.base import (
    LLMBadOutput,
    LLMError,
    LLMResponse,
    LLMUnavailable,
    Message,
    ToolCall,
    ToolSpec,
)


def _messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        if message.role == "user":
            out.append({"role": "user", "content": message.text})
        elif message.role == "assistant":
            out.append(
                {
                    "role": "assistant",
                    "content": message.text or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.args)},
                        }
                        for c in message.tool_calls
                    ],
                }
            )
        else:
            out += [
                {"role": "tool", "tool_call_id": r.id, "content": json.dumps(r.content)}
                for r in message.tool_results
            ]
    return out


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self, base_url: str, api_key: str, client: httpx.AsyncClient | None = None
    ) -> None:
        if not base_url:
            raise LLMError("OPENAI_COMPAT_BASE_URL is not set")
        self._base_url = base_url.rstrip("/")
        self._key = api_key
        self._client = client or httpx.AsyncClient()

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        timeout_seconds: float,
    ) -> LLMResponse:
        body = {
            "model": model,
            "messages": _messages(system, messages),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.schema,
                    },
                }
                for t in tools
            ],
            "tool_choice": "required",
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=body,
                timeout=timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMUnavailable(f"{model}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            detail = response.text[:300]
            if response.status_code in (404, 429) or response.status_code >= 500:
                retry = response.headers.get("retry-after")
                raise LLMUnavailable(
                    f"{model}: {response.status_code} {detail}",
                    float(retry) if retry and retry.replace(".", "", 1).isdigit() else None,
                )
            raise LLMError(f"{model}: {response.status_code} {detail}")
        data = response.json()
        choice = (data.get("choices") or [{}])[0].get("message") or {}
        calls: list[ToolCall] = []
        for call in choice.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError as exc:
                raise LLMBadOutput(f"{model}: tool arguments were not valid JSON") from exc
            calls.append(
                ToolCall(
                    id=call.get("id") or uuid.uuid4().hex[:12],
                    name=function.get("name", ""),
                    args=args if isinstance(args, dict) else {},
                )
            )
        if not calls:
            raise LLMBadOutput(f"{model}: no tool call")
        usage = data.get("usage") or {}
        return LLMResponse(
            message=Message(
                role="assistant",
                text=choice.get("content") or "",
                tool_calls=calls,
                provider=self.name,
            ),
            model=model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )
