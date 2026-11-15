"""Provider-neutral types for a tool-calling conversation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class LLMError(Exception):
    """The request was refused and retrying won't help (bad key, bad request)."""


class LLMUnavailable(LLMError):
    """Rate limit, outage or timeout: worth trying again, or trying another model."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMBadOutput(LLMError):
    """The model answered, but not with something usable (e.g. a malformed tool call)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema of the arguments


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolOutcome:
    id: str
    name: str
    content: dict[str, Any]


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolOutcome] = field(default_factory=list)
    # The provider's own form of an assistant turn. Sent back unchanged to the same provider,
    # which some models require (Gemini's thought signatures); ignored by any other provider.
    raw: Any = None
    provider: str = ""


@dataclass
class LLMResponse:
    message: Message
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.message.tool_calls


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        timeout_seconds: float,
    ) -> LLMResponse:
        """One model turn. The model must answer with at least one tool call."""
        ...
