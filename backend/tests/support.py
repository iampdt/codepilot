"""Test doubles: a scripted model and a sandbox that is a temporary directory.

`LocalSandbox` runs the real file agent (`sandbox/image/agentd.py`) against a temp directory
and runs commands with the host shell. It exists only here, for tests that script every
command themselves; the application has no code path that executes anything on the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.llm.base import LLMResponse, Message, ToolCall, ToolSpec
from app.sandbox.base import (
    ExecResult,
    IsolationReport,
    OutputCallback,
    SandboxLimits,
    SandboxToolError,
)

AGENTD = Path(__file__).resolve().parents[2] / "sandbox" / "image" / "agentd.py"


class LocalSandbox:
    def __init__(self, task_id: str) -> None:
        self.id = f"local-{task_id}"
        self.root = Path(tempfile.mkdtemp(prefix="cp-test-")).resolve()
        self.isolation = IsolationReport(driver="local-test", layers=["none (test double)"])
        self.destroyed = False
        self.commands: list[tuple[str, bool]] = []

    def _env(self) -> dict[str, str]:
        # The interpreter running the tests comes first, so `python -m pytest` finds pytest.
        path = os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"]
        return {"PATH": path, "HOME": str(self.root), "CODEPILOT_WORKSPACE": str(self.root),
                "PYTHONDONTWRITEBYTECODE": "1"}  # fmt: skip

    async def _agentd(self, *argv: str, stdin: bytes) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(AGENTD), *argv,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._env(),
        )  # fmt: skip
        out, _ = await proc.communicate(stdin)
        return out

    async def file_op(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        reply = json.loads(await self._agentd(stdin=json.dumps({"op": op, "args": args}).encode()))
        if not reply["ok"]:
            raise SandboxToolError(reply["error"]["code"], reply["error"]["message"])
        return reply["result"]

    async def exec(
        self,
        command: str,
        *,
        timeout: int,
        network: bool = False,
        env: dict[str, str] | None = None,
        on_output: OutputCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> ExecResult:
        self.commands.append((command, network))
        started = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command, cwd=self.root, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={**self._env(), **(env or {})},
        )  # fmt: skip
        timed_out = False
        work = asyncio.create_task(proc.communicate())
        stop = asyncio.create_task(cancel.wait()) if cancel else None
        done, _ = await asyncio.wait(
            {work, *([stop] if stop else [])}, timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )  # fmt: skip
        if work not in done:
            timed_out = not (stop and stop in done)
            proc.kill()
        if stop:
            stop.cancel()
        out, _ = await work
        text = out.decode(errors="replace")
        if on_output and text:
            await on_output(text)
        return ExecResult(
            exit_code=124 if timed_out else (proc.returncode or 0),
            output=text,
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - started) * 1000),
            cancelled=bool(cancel and cancel.is_set()),
        )

    async def unpack(self, tar_bytes: bytes) -> dict[str, Any]:
        return json.loads(await self._agentd("unpack", stdin=tar_bytes))["result"]

    async def pack(self) -> bytes:
        return await self._agentd("pack", stdin=b"")

    async def stats(self) -> dict[str, Any]:
        return {}

    async def destroy(self) -> None:
        self.destroyed = True
        shutil.rmtree(self.root, ignore_errors=True)


class LocalDriver:
    name = "local-test"

    def __init__(self) -> None:
        self.sandboxes: dict[str, LocalSandbox] = {}

    async def create(self, task_id: str, limits: SandboxLimits) -> LocalSandbox:
        self.sandboxes[task_id] = LocalSandbox(task_id)
        return self.sandboxes[task_id]

    async def attach(self, task_id: str) -> LocalSandbox | None:
        sandbox = self.sandboxes.get(task_id)
        return sandbox if sandbox and not sandbox.destroyed else None

    async def cleanup(self, keep_task_ids: set[str]) -> int:
        return 0


Script = Callable[[str, list[Message], list[ToolSpec]], list[ToolCall]]


class ScriptedProvider:
    """A model whose every turn is decided by a function, so a whole run is deterministic."""

    name = "scripted"

    def __init__(self, script: Script) -> None:
        self.script = script
        self.calls: list[tuple[str, list[str]]] = []

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        timeout_seconds: float,
    ) -> LLMResponse:
        role = agent_of(tools)
        self.calls.append((role, [t.name for t in tools]))
        calls = self.script(role, messages, tools)
        return LLMResponse(
            message=Message(role="assistant", tool_calls=calls, provider=self.name),
            model=model,
            input_tokens=100,
            output_tokens=20,
        )


def agent_of(tools: list[ToolSpec]) -> str:
    submit = next(t.name for t in tools if t.name.startswith("submit_"))
    return {
        "submit_plan": "planner",
        "submit_code": "coder",
        "submit_execution": "executor",
        "submit_debug": "debugger",
        "submit_review": "reviewer",
    }[submit]


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"c{time.time_ns()}", name=name, args=args)


def turns(messages: list[Message]) -> int:
    """How many turns this agent has already taken in the conversation."""
    return sum(1 for m in messages if m.role == "assistant")


def last_results(messages: list[Message]) -> list[dict[str, Any]]:
    for message in reversed(messages):
        if message.role == "tool":
            return [r.content for r in message.tool_results]
    return []


PLAN = {
    "summary": "Add a small calculator module with an add function and tests.",
    "approach": "One module, one test file, run with pytest.",
    "steps": [
        {"id": 1, "title": "Write calc.py", "description": "add(a, b) returns the sum.",
         "files": ["calc.py"]},
        {"id": 2, "title": "Write tests", "description": "Test add with pytest.",
         "files": ["test_calc.py"]},
    ],
    "acceptance_criteria": ["add(2, 3) returns 5", "tests pass"],
    "risks": [],
    "dependencies": [],
    "test_strategy": "pytest unit tests",
    "test_command": "python -m pytest -q",
}  # fmt: skip

REVIEW_OK = {
    "verdict": "approve",
    "summary": "The change does what was asked and the tests cover it.",
    "criteria": [{"criterion": "add(2, 3) returns 5", "met": True, "evidence": "test_calc.py"}],
    "findings": [],
}
