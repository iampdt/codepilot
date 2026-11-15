"""The bounded agent loop shared by all five agents.

One loop, parameterised by an `AgentSpec`: a role (which decides the tools, through the
registry's permission table), a system prompt, a step limit, and the contract the agent must
submit. The loop ends in exactly three ways: the agent submits a valid contract, the step
limit forces a submit-only turn, or the task's budget/cancellation ends the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.llm.base import LLMResponse, Message, ToolCall, ToolOutcome, ToolSpec
from app.llm.router import ModelRouter
from app.logging import get_logger
from app.tools.registry import TaskCancelled, ToolContext, invoke, tools_for

log = get_logger("agent")

# Turns older than this many messages have their bulky content replaced by a stub. An agent
# that needs an old file again reads it again; that is cheaper than resending it every turn.
KEEP_RECENT_MESSAGES = 8
ELIDE_OVER_CHARS = 1200
FINAL_SUBMIT_ATTEMPTS = 2


class AgentFailed(Exception):
    """The agent could not produce its contract within its limits."""


@dataclass(frozen=True)
class AgentSpec:
    role: str
    system: str
    submit_tool: str
    submit_description: str
    max_steps: int


def _elide_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > ELIDE_OVER_CHARS:
        return f"[{len(value)} characters elided to save context]"
    if isinstance(value, dict):
        return {k: _elide_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_elide_value(v) for v in value]
    return value


def compact(messages: list[Message]) -> list[Message]:
    """A copy of the history with old, bulky tool arguments and results stubbed out.

    The first message (the brief) and the most recent turns are left whole."""
    cutoff = len(messages) - KEEP_RECENT_MESSAGES
    if cutoff <= 1:
        return messages
    out: list[Message] = [messages[0]]
    for message in messages[1:cutoff]:
        if message.role == "tool":
            out.append(
                Message(
                    role="tool",
                    tool_results=[
                        ToolOutcome(r.id, r.name, _elide_value(r.content))
                        for r in message.tool_results
                    ],
                )
            )
        elif message.role == "assistant":
            slim = copy.copy(message)
            slim.tool_calls = [
                ToolCall(c.id, c.name, _elide_value(c.args)) for c in message.tool_calls
            ]
            if isinstance(message.raw, list):
                raw = copy.deepcopy(message.raw)
                for part in raw:
                    call = part.get("functionCall") if isinstance(part, dict) else None
                    if call and isinstance(call.get("args"), dict):
                        call["args"] = _elide_value(call["args"])
                slim.raw = raw
            out.append(slim)
        else:
            out.append(message)
    return out + messages[cutoff:]


async def _call_model(
    router: ModelRouter, ctx: ToolContext, system: str, messages: list[Message],
    tools: list[ToolSpec],
) -> LLMResponse:  # fmt: skip
    """One model call that ends early if the task is cancelled."""

    async def notice(error: str, wait: float) -> None:
        await ctx.bus.emit(
            ctx.task_id,
            "llm_retry",
            {"agent": ctx.agent, "error": error, "wait_seconds": round(wait, 1)},
        )

    call = asyncio.create_task(
        router.complete(system=system, messages=compact(messages), tools=tools, on_retry=notice)
    )
    cancelled = asyncio.create_task(ctx.cancel.wait())
    try:
        await asyncio.wait({call, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        if not call.done():
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call
            raise TaskCancelled()
        return call.result()
    finally:
        cancelled.cancel()


async def run_agent[T: BaseModel](
    spec: AgentSpec,
    *,
    brief: str,
    ctx: ToolContext,
    router: ModelRouter,
    output_model: type[T],
) -> T:
    ctx.for_agent(spec.role)
    role_tools = [
        ToolSpec(t.name, t.description, t.input_model.model_json_schema())
        for t in tools_for(spec.role)
    ]
    submit = ToolSpec(spec.submit_tool, spec.submit_description, output_model.model_json_schema())
    messages: list[Message] = [Message(role="user", text=brief)]
    await ctx.bus.emit(
        ctx.task_id,
        "agent_started",
        {"agent": spec.role, "max_steps": spec.max_steps,
         "tools": [t.name for t in role_tools]},
    )  # fmt: skip

    total_steps = spec.max_steps + FINAL_SUBMIT_ATTEMPTS
    for step in range(1, total_steps + 1):
        submit_only = step > spec.max_steps
        if step == spec.max_steps - 2 and not submit_only:
            messages.append(
                Message(
                    role="user",
                    text=f"You have 3 steps left. Finish what is essential, then call "
                    f"{spec.submit_tool}.",
                )
            )
        if step == spec.max_steps + 1:
            messages.append(
                Message(
                    role="user",
                    text=f"Step limit reached. Call {spec.submit_tool} now with the work as it "
                    "stands; say plainly what is unfinished.",
                )
            )

        ctx.budget.before_llm_call()
        started = time.monotonic()
        response = await _call_model(
            router, ctx, spec.system, messages, [submit] if submit_only else [*role_tools, submit]
        )
        ctx.budget.charge_llm(spec.role, response.input_tokens, response.output_tokens)
        await ctx.bus.emit(
            ctx.task_id,
            "llm_call",
            {
                "agent": spec.role,
                "step": step,
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "budget": ctx.budget.snapshot(),
            },
        )
        messages.append(response.message)
        if response.message.text.strip():
            await ctx.bus.emit(
                ctx.task_id,
                "agent_note",
                {"agent": spec.role, "text": response.message.text.strip()[:2000]},
            )

        outcomes: list[ToolOutcome] = []
        submitted: T | None = None
        for call in response.tool_calls:
            if call.name == spec.submit_tool:
                try:
                    submitted = output_model.model_validate(call.args)
                    outcomes.append(ToolOutcome(call.id, call.name, {"ok": True}))
                except ValidationError as exc:
                    problems = "; ".join(
                        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                        for e in exc.errors()[:8]
                    )
                    outcomes.append(
                        ToolOutcome(
                            call.id,
                            call.name,
                            {"ok": False, "error": {"code": "invalid_contract",
                                                    "message": problems}},
                        )
                    )  # fmt: skip
                    await ctx.bus.emit(
                        ctx.task_id,
                        "contract_rejected",
                        {"agent": spec.role, "problems": problems},
                    )
            elif submit_only:
                outcomes.append(
                    ToolOutcome(
                        call.id,
                        call.name,
                        {"ok": False, "error": {"code": "step_limit",
                                                "message": "only the submit tool is available"}},
                    )
                )  # fmt: skip
            else:
                result = await invoke(ctx, call.name, call.args)
                outcomes.append(ToolOutcome(call.id, call.name, result.for_model()))
        messages.append(Message(role="tool", tool_results=outcomes))

        if submitted is not None:
            await ctx.bus.emit(
                ctx.task_id,
                "agent_finished",
                {"agent": spec.role, "steps": step, "output": submitted.model_dump()},
            )
            log.info("agent_finished", task_id=ctx.task_id, agent=spec.role, steps=step)
            return submitted

    raise AgentFailed(f"the {spec.role} agent did not submit a valid result in {total_steps} steps")
