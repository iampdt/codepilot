"""Model router: a chain of models, tried in order, with waits between rounds.

Free tiers fail in two ways: a per-minute rate limit (wait and the same model works again)
and an overloaded or missing model (the next model works now). So each round tries every
model once, and only if all of them are unavailable does the router wait before the next
round. The wait is the provider's own retry hint when it gives one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.config import Settings
from app.llm.base import (
    LLMBadOutput,
    LLMError,
    LLMProvider,
    LLMResponse,
    LLMUnavailable,
    Message,
    ToolSpec,
)
from app.logging import get_logger

log = get_logger("llm")

RetryNotice = Callable[[str, float], Awaitable[None]]
MAX_ROUNDS = 4
MAX_WAIT_SECONDS = 65.0


class LLMExhausted(LLMError):
    """Every model in the chain stayed unavailable for every round."""


class ModelRouter:
    def __init__(
        self, provider: LLMProvider, models: list[str], *, timeout_seconds: float = 120.0
    ) -> None:
        if not models:
            raise LLMError("no models configured")
        self.provider = provider
        self.models = models
        self._timeout = timeout_seconds

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        on_retry: RetryNotice | None = None,
    ) -> LLMResponse:
        last_error: Exception | None = None
        for round_number in range(MAX_ROUNDS):
            hint = 0.0
            for model in self.models:
                try:
                    return await self.provider.complete(
                        model=model,
                        system=system,
                        messages=messages,
                        tools=tools,
                        timeout_seconds=self._timeout,
                    )
                except LLMUnavailable as exc:
                    last_error = exc
                    hint = max(hint, exc.retry_after or 0.0)
                    log.warning("llm_unavailable", model=model, error=str(exc)[:200])
                except LLMBadOutput as exc:
                    # A malformed answer is usually a one-off: the next model, or the next
                    # round, gets the same request again.
                    last_error = exc
                    log.warning("llm_bad_output", model=model, error=str(exc)[:200])
            if round_number == MAX_ROUNDS - 1:
                break
            wait = min(max(hint, 5.0 * 2**round_number), MAX_WAIT_SECONDS)
            if on_retry is not None:
                await on_retry(str(last_error)[:200], wait)
            await asyncio.sleep(wait)
        raise LLMExhausted(f"all models unavailable: {last_error}")


def build_router(settings: Settings) -> ModelRouter:
    provider: LLMProvider
    if settings.llm_provider == "gemini":
        from app.llm.gemini import GeminiProvider

        provider = GeminiProvider(settings.gemini_api_key)
    elif settings.llm_provider == "openai_compat":
        from app.llm.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(
            settings.openai_compat_base_url, settings.openai_compat_api_key
        )
    else:
        raise LLMError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")
    return ModelRouter(provider, settings.model_chain, timeout_seconds=settings.llm_timeout_seconds)
