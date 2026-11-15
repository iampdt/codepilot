"""Gemini over its REST API (free tier). Plain HTTP keeps the wire format visible: the
assistant turn is stored exactly as received and sent back exactly as stored."""

from __future__ import annotations

import re
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

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# The subset of JSON Schema that Gemini's function declarations accept.
_KEEP = {"type", "description", "properties", "required", "items", "enum", "nullable"}


def to_gemini_schema(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reduce a Pydantic JSON Schema to what Gemini accepts: inline $ref, turn
    `anyOf: [X, null]` into a nullable X, and drop keywords it rejects."""
    defs = defs if defs is not None else schema.get("$defs", {})
    if "$ref" in schema:
        return to_gemini_schema(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if o.get("type") != "null"]
        reduced: dict[str, Any] = (
            to_gemini_schema(options[0], defs) if options else {"type": "string"}
        )
        reduced["nullable"] = True
        if "description" in schema:
            reduced["description"] = schema["description"]
        return reduced
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _KEEP:
            continue
        if key == "properties":
            out[key] = {name: to_gemini_schema(sub, defs) for name, sub in value.items()}
        elif key == "items":
            out[key] = to_gemini_schema(value, defs)
        else:
            out[key] = value
    return out


def _contents(messages: list[Message]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "user":
            contents.append({"role": "user", "parts": [{"text": message.text}]})
        elif message.role == "assistant":
            if message.provider == "gemini" and message.raw:
                contents.append({"role": "model", "parts": message.raw})
                continue
            parts: list[dict[str, Any]] = [{"text": message.text}] if message.text else []
            parts += [
                {"functionCall": {"name": c.name, "args": c.args}} for c in message.tool_calls
            ]
            contents.append({"role": "model", "parts": parts})
        else:
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {"functionResponse": {"name": r.name, "response": r.content}}
                        for r in message.tool_results
                    ],
                }
            )
    return contents


def _retry_after(body: dict[str, Any]) -> float | None:
    for detail in body.get("error", {}).get("details", []):
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and (match := re.match(r"^([\d.]+)s$", delay)):
            return float(match.group(1))
    return None


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
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
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": _contents(messages),
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": to_gemini_schema(t.schema),
                        }
                        for t in tools
                    ]
                }
            ],
            # ANY: the model must call a tool. Agents finish by calling a submit tool, so a
            # turn with no call is never a valid answer.
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 16384},
        }
        try:
            response = await self._client.post(
                f"{BASE_URL}/models/{model}:generateContent",
                headers={"x-goog-api-key": self._key},
                json=body,
                timeout=timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMUnavailable(f"{model}: {type(exc).__name__}") from exc

        if response.status_code != 200:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            detail = str(payload.get("error", {}).get("message", ""))[:300]
            if response.status_code in (404, 429) or response.status_code >= 500:
                raise LLMUnavailable(
                    f"{model}: {response.status_code} {detail}", _retry_after(payload)
                )
            raise LLMError(f"{model}: {response.status_code} {detail}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMBadOutput(f"{model}: no candidates ({data.get('promptFeedback')})")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        calls = [
            ToolCall(
                id=part["functionCall"].get("id") or uuid.uuid4().hex[:12],
                name=part["functionCall"].get("name", ""),
                args=dict(part["functionCall"].get("args") or {}),
            )
            for part in parts
            if "functionCall" in part
        ]
        if not calls:
            raise LLMBadOutput(f"{model}: no tool call (finish: {candidate.get('finishReason')})")
        text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
        usage = data.get("usageMetadata") or {}
        return LLMResponse(
            message=Message(
                role="assistant", text=text, tool_calls=calls, raw=parts, provider=self.name
            ),
            model=model,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0)
            + int(usage.get("thoughtsTokenCount") or 0),
        )
