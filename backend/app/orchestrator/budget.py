"""Execution budget: hard ceilings on what one task may spend.

Every model call and tool call is charged here before it happens. When a ceiling is reached
the next charge raises BudgetExceeded and the orchestrator ends the run; there is no path by
which an agent loop can keep going past its budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class BudgetExceeded(Exception):
    def __init__(self, what: str, limit: int) -> None:
        super().__init__(f"budget exceeded: {what} (limit {limit})")
        self.what = what
        self.limit = limit


@dataclass
class BudgetLimits:
    max_llm_calls: int
    max_total_tokens: int
    max_tool_calls: int
    max_wall_seconds: int


@dataclass
class Budget:
    limits: BudgetLimits
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    by_agent: dict[str, dict[str, int]] = field(default_factory=dict)
    # Wall time already spent in earlier runs of this task (before an approval pause).
    elapsed_before: float = 0.0
    _started: float = field(default_factory=time.monotonic)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_seconds(self) -> float:
        return self.elapsed_before + (time.monotonic() - self._started)

    def _agent(self, agent: str) -> dict[str, int]:
        return self.by_agent.setdefault(
            agent, {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "tool_calls": 0}
        )

    def check(self) -> None:
        if self.elapsed_seconds > self.limits.max_wall_seconds:
            raise BudgetExceeded("wall-clock time", self.limits.max_wall_seconds)
        if self.total_tokens > self.limits.max_total_tokens:
            raise BudgetExceeded("tokens", self.limits.max_total_tokens)

    def before_llm_call(self) -> None:
        self.check()
        if self.llm_calls >= self.limits.max_llm_calls:
            raise BudgetExceeded("model calls", self.limits.max_llm_calls)

    def charge_llm(self, agent: str, input_tokens: int, output_tokens: int) -> None:
        self.llm_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        usage = self._agent(agent)
        usage["llm_calls"] += 1
        usage["input_tokens"] += input_tokens
        usage["output_tokens"] += output_tokens

    def charge_tool(self, agent: str) -> None:
        self.check()
        if self.tool_calls >= self.limits.max_tool_calls:
            raise BudgetExceeded("tool calls", self.limits.max_tool_calls)
        self.tool_calls += 1
        self._agent(agent)["tool_calls"] += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "by_agent": self.by_agent,
            "limits": {
                "max_llm_calls": self.limits.max_llm_calls,
                "max_total_tokens": self.limits.max_total_tokens,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_wall_seconds": self.limits.max_wall_seconds,
            },
        }

    @classmethod
    def restore(cls, limits: BudgetLimits, usage: dict[str, Any] | None) -> Budget:
        usage = usage or {}
        return cls(
            limits=limits,
            llm_calls=int(usage.get("llm_calls", 0)),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            tool_calls=int(usage.get("tool_calls", 0)),
            by_agent=dict(usage.get("by_agent", {})),
            elapsed_before=float(usage.get("elapsed_seconds", 0.0)),
        )
