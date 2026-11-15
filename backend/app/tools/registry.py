"""The tool system: everything an agent can do, and the checks in front of each action.

A tool call goes through the same five steps whatever the tool is:

  1. permission  – the calling agent's role must hold the tool's permission;
  2. validation  – arguments are parsed into the tool's Pydantic input model;
  3. budget      – the call is charged against the task's tool budget;
  4. execution   – the handler runs under a timeout, against the sandbox only;
  5. record      – the outcome is emitted as an event and returned as a structured result.

Failures at any step come back to the model as `{"ok": false, "error": {...}}` so it can
correct itself; they never raise into the agent loop. The exceptions are budget exhaustion,
cancellation and sandbox failure, which end the run.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.events import EventBus
from app.logging import get_logger
from app.models import Execution
from app.orchestrator.budget import Budget
from app.sandbox import git
from app.sandbox.base import ExecResult, Sandbox, SandboxToolError, Workspace
from app.sandbox.policy import check_command, check_package_spec, is_test_command

log = get_logger("tools")


class Permission(StrEnum):
    FS_READ = "fs_read"
    FS_WRITE = "fs_write"
    EXEC = "exec"
    INSTALL = "install"
    LOGS = "logs"
    DIFF = "diff"
    CHECKPOINT = "checkpoint"


P = Permission

# What each agent role may do. The planner and the reviewer can't change anything; the coder
# can't run anything; only the executor and the debugger can run commands.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "planner": frozenset({P.FS_READ}),
    "coder": frozenset({P.FS_READ, P.FS_WRITE, P.DIFF, P.CHECKPOINT}),
    "executor": frozenset({P.FS_READ, P.EXEC, P.INSTALL, P.LOGS}),
    "debugger": frozenset({P.FS_READ, P.FS_WRITE, P.EXEC, P.INSTALL, P.LOGS, P.DIFF, P.CHECKPOINT}),
    "reviewer": frozenset({P.FS_READ, P.DIFF, P.LOGS}),
}


class QuotaExceeded(Exception):
    """The workspace grew past its disk quota; the run ends."""


class TaskCancelled(Exception):
    pass


@dataclass
class ToolContext:
    task_id: str
    agent: str
    workspace: Workspace
    bus: EventBus
    budget: Budget
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    cancel: asyncio.Event
    sandbox: Sandbox | None = None
    base_commit: str | None = None
    test_command: str | None = None
    # Checkpoints this task created: id → label. restore_checkpoint accepts nothing else.
    checkpoints: dict[str, str] = field(default_factory=dict)
    # Result of the most recent test run, as observed by the tool layer (not as reported).
    last_test: dict[str, Any] | None = None

    def for_agent(self, agent: str) -> ToolContext:
        self.agent = agent
        return self


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None

    def for_model(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, **self.data}
        return {"ok": False, "error": self.error}


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


Handler = Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[ToolInput]
    permission: Permission
    handler: Handler
    timeout_seconds: int = 30


# --- helpers -----------------------------------------------------------------------------

RELATIVE_PATH = re.compile(r"^[^\x00]{1,400}$")


def _check_path(path: str) -> None:
    """Syntactic check on the control plane; the file agent resolves symlinks and checks again."""
    if not RELATIVE_PATH.match(path) or path.startswith(("/", "~")):
        raise SandboxToolError("path_outside_workspace", "use a path relative to the workspace")
    if ".." in path.replace("\\", "/").split("/"):
        raise SandboxToolError("path_outside_workspace", "'..' is not allowed in paths")


def _clip(text: str, head: int = 1500, tail: int = 5000) -> str:
    """Command output as the model sees it: the start, and most of the end."""
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n… [{len(text) - head - tail} characters omitted] …\n{text[-tail:]}"


_PYTEST_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


def parse_test_summary(output: str) -> dict[str, int]:
    """Counts from a pytest-style summary line, when there is one."""
    counts: dict[str, int] = {}
    for line in reversed(output.splitlines()[-15:]):
        found = _PYTEST_SUMMARY.findall(line)
        if found:
            for number, label in found:
                counts[label.rstrip("s") if label == "errors" else label] = int(number)
            break
    return counts


_ERROR_LINE = re.compile(r"^E\s{2,}(\S.*)$")
_FAILURE_HEADER = re.compile(r"^_{3,} .+ _{3,}$")
_FAILED_TEST = re.compile(r"^(?:FAILED|ERROR) (\S+)")


def digest_failures(output: str) -> dict[str, Any]:
    """What a failing test run says, without the bulk.

    A real failure can be 100 KB of repeated tracebacks and warnings, and the end of it is
    the least useful part. This keeps each distinct error line once, the names of the failing
    tests, and the first traceback whole: enough to find the cause, small enough to read."""
    lines = output.splitlines()
    errors: list[str] = []
    failed: list[str] = []
    for line in lines:
        if (match := _ERROR_LINE.match(line)) and match.group(1)[:300] not in errors:
            errors.append(match.group(1)[:300])
        elif (match := _FAILED_TEST.match(line)) and match.group(1) not in failed:
            failed.append(match.group(1))
    first = ""
    starts = [i for i, line in enumerate(lines) if _FAILURE_HEADER.match(line)]
    if starts:
        end = starts[1] if len(starts) > 1 else len(lines)
        block = "\n".join(lines[starts[0] : end])
        first = block if len(block) <= 4000 else f"{block[:1200]}\n…\n{block[-2800:]}"
    return {"errors": errors[:25], "failed_tests": failed[:40], "first_failure": first}


def format_failure(output: str) -> str:
    """A failing run as text for a brief: the digest when there is one, else the tail."""
    digest = digest_failures(output)
    if not digest["errors"] and not digest["first_failure"]:
        return _clip(output, head=1500, tail=7000)
    parts = []
    if digest["failed_tests"]:
        parts.append("Failing tests:\n" + "\n".join(digest["failed_tests"]))
    if digest["errors"]:
        parts.append("Distinct error lines:\n" + "\n".join(digest["errors"]))
    if digest["first_failure"]:
        parts.append("First failure in full:\n" + digest["first_failure"])
    parts.append("End of output:\n" + output[-1200:])
    return "\n\n".join(parts)


def _require_sandbox(ctx: ToolContext) -> Sandbox:
    if ctx.sandbox is None:
        raise SandboxToolError("no_sandbox", "this tool is only available after plan approval")
    return ctx.sandbox


class _Streamer:
    """Coalesces command output into events so a chatty command doesn't flood the stream."""

    MAX_EVENTS = 400

    def __init__(self, ctx: ToolContext, run_id: str) -> None:
        self._ctx = ctx
        self._run_id = run_id
        self._pending = ""
        self._last_flush = time.monotonic()
        self._events = 0

    async def __call__(self, chunk: str) -> None:
        self._pending += chunk
        if len(self._pending) >= 2000 or time.monotonic() - self._last_flush >= 0.3:
            await self.flush()

    async def flush(self) -> None:
        if not self._pending or self._events >= self.MAX_EVENTS:
            self._pending = ""
            return
        text, self._pending = self._pending, ""
        self._last_flush = time.monotonic()
        self._events += 1
        await self._ctx.bus.emit(
            self._ctx.task_id, "command_output", {"run_id": self._run_id, "text": text}
        )


async def run_in_sandbox(
    ctx: ToolContext, command: str, *, kind: str, timeout: int, network: bool = False
) -> tuple[ExecResult, int]:
    """Run a command, stream its output, store the execution, and enforce the disk quota."""
    sandbox = _require_sandbox(ctx)
    run_id = f"{ctx.agent}-{time.time_ns()}"
    await ctx.bus.emit(
        ctx.task_id,
        "command_started",
        {"run_id": run_id, "agent": ctx.agent, "kind": kind, "command": command,
         "network": network},
    )  # fmt: skip
    streamer = _Streamer(ctx, run_id)
    result = await sandbox.exec(
        command, timeout=timeout, network=network, on_output=streamer, cancel=ctx.cancel
    )
    await streamer.flush()
    async with ctx.sessions() as session:
        row = Execution(
            task_id=ctx.task_id,
            agent=ctx.agent,
            kind=kind,
            command=command,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            output=result.output.replace("\x00", ""),
        )
        session.add(row)
        await session.commit()
        execution_id = row.id
    await ctx.bus.emit(
        ctx.task_id,
        "command_finished",
        {"run_id": run_id, "execution_id": execution_id, "exit_code": result.exit_code,
         "timed_out": result.timed_out, "duration_ms": result.duration_ms},
    )  # fmt: skip
    if result.cancelled:
        raise TaskCancelled()
    usage = await sandbox.file_op("stat", {})
    if usage["bytes"] > ctx.settings.sandbox_workspace_mb * 1024 * 1024:
        raise QuotaExceeded(
            f"workspace is {usage['bytes'] // (1024 * 1024)} MB, over the "
            f"{ctx.settings.sandbox_workspace_mb} MB quota"
        )
    return result, execution_id


def _exec_payload(result: ExecResult, execution_id: int) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "execution_id": execution_id,
        "output": _clip(result.output),
    }


# --- file tools --------------------------------------------------------------------------


class ListFilesInput(ToolInput):
    path: str = Field(default=".", description="Directory to list, relative to the workspace.")


class ReadFileInput(ToolInput):
    path: str = Field(description="File to read, relative to the workspace.")
    start_line: int | None = Field(default=None, ge=1, description="First line (1-based).")
    end_line: int | None = Field(default=None, ge=1, description="Last line, inclusive.")


class WriteFileInput(ToolInput):
    path: str = Field(description="File to create or overwrite, relative to the workspace.")
    content: str = Field(description="The complete new content of the file.")


class EditFileInput(ToolInput):
    path: str = Field(description="File to edit, relative to the workspace.")
    old_string: str = Field(description="Exact text to replace; must match the file exactly.")
    new_string: str = Field(description="Text to put in its place.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class DeleteFileInput(ToolInput):
    path: str = Field(description="File or directory to delete, relative to the workspace.")


class SearchCodeInput(ToolInput):
    pattern: str = Field(description="Text to search for.")
    regex: bool = Field(default=False, description="Treat pattern as a regular expression.")
    path: str = Field(default=".", description="Directory or file to search in.")
    glob: str | None = Field(default=None, description="Only file names matching, e.g. *.py")


async def _list_files(ctx: ToolContext, args: ListFilesInput) -> dict[str, Any]:
    _check_path(args.path)
    result = await ctx.workspace.file_op("list_files", {"path": args.path, "max_entries": 400})
    # A compact listing costs far fewer tokens than a list of objects.
    lines = [
        e["path"] if e["type"] == "dir" else f"{e['path']} ({e['size']} bytes)"
        for e in result["entries"]
    ]
    return {"files": lines, "truncated": result["truncated"]}


async def _read_file(ctx: ToolContext, args: ReadFileInput) -> dict[str, Any]:
    _check_path(args.path)
    result = await ctx.workspace.file_op("read_file", args.model_dump(exclude_none=True))
    if len(result["content"]) > 24_000:
        result["content"] = result["content"][:24_000]
        result["truncated"] = True
        result["note"] = "file is long: read the rest with start_line/end_line"
    return result


async def _write_file(ctx: ToolContext, args: WriteFileInput) -> dict[str, Any]:
    _check_path(args.path)
    result = await ctx.workspace.file_op("write_file", args.model_dump())
    await ctx.bus.emit(
        ctx.task_id,
        "file_changed",
        {"agent": ctx.agent, "path": result["path"],
         "action": "created" if result["created"] else "modified"},
    )  # fmt: skip
    return result


async def _edit_file(ctx: ToolContext, args: EditFileInput) -> dict[str, Any]:
    _check_path(args.path)
    result = await ctx.workspace.file_op("edit_file", args.model_dump())
    await ctx.bus.emit(
        ctx.task_id,
        "file_changed",
        {"agent": ctx.agent, "path": result["path"], "action": "modified"},
    )
    return result


async def _delete_file(ctx: ToolContext, args: DeleteFileInput) -> dict[str, Any]:
    _check_path(args.path)
    result = await ctx.workspace.file_op("delete_file", args.model_dump())
    await ctx.bus.emit(
        ctx.task_id,
        "file_changed",
        {"agent": ctx.agent, "path": result["path"], "action": "deleted"},
    )
    return result


async def _search_code(ctx: ToolContext, args: SearchCodeInput) -> dict[str, Any]:
    _check_path(args.path)
    result = await ctx.workspace.file_op("search_code", args.model_dump(exclude_none=True))
    lines = [f"{m['path']}:{m['line']}: {m['text']}" for m in result["matches"]]
    return {"matches": lines, "truncated": result["truncated"]}


# --- execution tools ---------------------------------------------------------------------


class ExecuteCommandInput(ToolInput):
    command: str = Field(
        description="One shell command, run in the workspace root with no network access."
    )
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)


class InstallDependencyInput(ToolInput):
    manager: Literal["pip", "npm"] = Field(description="Which package manager to use.")
    packages: list[str] = Field(
        default_factory=list,
        max_length=40,
        description="Package names, optionally with versions, e.g. fastapi==0.115.0.",
    )
    requirements_file: str | None = Field(
        default=None,
        description="pip only: install from this requirements file instead of `packages`.",
    )
    directory: str = Field(
        default=".", description="Directory to run in (where package.json is, for npm)."
    )


class RunTestsInput(ToolInput):
    command: str | None = Field(
        default=None,
        description="Test command, e.g. `pytest -q`. Defaults to the task's test command.",
    )


class GetExecutionLogsInput(ToolInput):
    execution_id: int | None = Field(
        default=None, description="An execution id from an earlier result; default is the latest."
    )
    tail_lines: int = Field(default=120, ge=1, le=400)


async def _execute_command(ctx: ToolContext, args: ExecuteCommandInput) -> dict[str, Any]:
    decision = check_command(args.command)
    if not decision.allowed:
        raise SandboxToolError("command_not_allowed", decision.reason)
    timeout = min(
        args.timeout_seconds or ctx.settings.command_timeout_seconds,
        ctx.settings.command_timeout_seconds,
    )
    result, execution_id = await run_in_sandbox(ctx, args.command, kind="command", timeout=timeout)
    return _exec_payload(result, execution_id)


async def _install_dependency(ctx: ToolContext, args: InstallDependencyInput) -> dict[str, Any]:
    _check_path(args.directory)
    if ctx.agent == "executor" and args.packages:
        # The executor can't edit files, so a package it installed by name would work here
        # and be missing from the exported project.
        raise SandboxToolError(
            "undeclared_dependency",
            "you may only install what the project declares (requirements_file, or npm with "
            "no packages). A missing package is a defect: report it as a failure",
        )
    for spec in args.packages:
        decision = check_package_spec(spec)
        if not decision.allowed:
            raise SandboxToolError("package_not_allowed", decision.reason)
    packages = " ".join(shlex.quote(p) for p in args.packages)
    if args.manager == "pip":
        if args.requirements_file:
            _check_path(args.requirements_file)
            command = f"pip install --no-input -r {shlex.quote(args.requirements_file)}"
        elif packages:
            command = f"pip install --no-input {packages}"
        else:
            raise SandboxToolError("invalid_input", "give packages or a requirements_file")
    else:
        if args.requirements_file:
            raise SandboxToolError("invalid_input", "requirements_file is for pip only")
        command = f"npm install --no-audit --no-fund {packages}".strip()
    if args.directory not in (".", ""):
        command = f"cd {shlex.quote(args.directory)} && {command}"
    result, execution_id = await run_in_sandbox(
        ctx, command, kind="install", timeout=ctx.settings.install_timeout_seconds, network=True
    )
    return _exec_payload(result, execution_id)


async def run_tests(ctx: ToolContext, command: str, *, kind: str = "test") -> dict[str, Any]:
    """Run a test command and record what was observed. Also used by the orchestrator."""
    result, execution_id = await run_in_sandbox(
        ctx, command, kind=kind, timeout=ctx.settings.test_timeout_seconds
    )
    summary = parse_test_summary(result.output)
    # pytest exits 5 when it collected no tests: that is not a pass.
    passed = result.ok
    ctx.last_test = {
        "command": command,
        "passed": passed,
        "exit_code": result.exit_code,
        "summary": summary,
        "execution_id": execution_id,
    }
    await ctx.bus.emit(
        ctx.task_id,
        "test_result",
        {"agent": ctx.agent, "kind": kind, **ctx.last_test, "duration_ms": result.duration_ms},
    )
    payload = _exec_payload(result, execution_id)
    if not passed:
        # Replace the raw tail with something a model can act on.
        payload["output"] = format_failure(result.output)
    return {"passed": passed, "summary": summary, **payload}


async def _run_tests(ctx: ToolContext, args: RunTestsInput) -> dict[str, Any]:
    command = args.command or ctx.test_command
    if not command:
        raise SandboxToolError("invalid_input", "no test command is known; pass one")
    decision = check_command(command)
    if not decision.allowed:
        raise SandboxToolError("command_not_allowed", decision.reason)
    if not is_test_command(command):
        raise SandboxToolError(
            "not_a_test_command",
            "run_tests only runs test runners (pytest, python -m unittest, npm test, "
            "npx vitest/jest, node --test); use execute_command for anything else",
        )
    return await run_tests(ctx, command)


async def _get_execution_logs(ctx: ToolContext, args: GetExecutionLogsInput) -> dict[str, Any]:
    async with ctx.sessions() as session:
        # Scoped to this task: an execution id from another task is simply not found.
        query = select(Execution).where(Execution.task_id == ctx.task_id)
        if args.execution_id is not None:
            query = query.where(Execution.id == args.execution_id)
        row = (await session.execute(query.order_by(Execution.id.desc()).limit(1))).scalar()
    if row is None:
        raise SandboxToolError("not_found", "no such execution for this task")
    lines = row.output.splitlines()
    return {
        "execution_id": row.id,
        "command": row.command,
        "exit_code": row.exit_code,
        "timed_out": row.timed_out,
        "total_lines": len(lines),
        "output": "\n".join(lines[-args.tail_lines :])[-12_000:],
    }


# --- version control tools ---------------------------------------------------------------


class InspectGitDiffInput(ToolInput):
    path: str | None = Field(default=None, description="Limit the diff to one file or directory.")
    stat_only: bool = Field(default=False, description="Only list changed files with counts.")


class CreateCheckpointInput(ToolInput):
    label: str = Field(min_length=1, max_length=80, description="Short name for this state.")


class RestoreCheckpointInput(ToolInput):
    checkpoint_id: str = Field(description="An id returned by create_checkpoint.")


async def _inspect_git_diff(ctx: ToolContext, args: InspectGitDiffInput) -> dict[str, Any]:
    sandbox = _require_sandbox(ctx)
    if args.path:
        _check_path(args.path)
    assert ctx.base_commit is not None
    text = await git.diff(sandbox, ctx.base_commit, args.path, stat=args.stat_only)
    truncated = len(text) > 30_000
    return {"diff": text[:30_000], "truncated": truncated}


async def create_checkpoint(ctx: ToolContext, label: str) -> str:
    sandbox = _require_sandbox(ctx)
    sha = await git.checkpoint(sandbox, label)
    ctx.checkpoints[sha[:12]] = label
    await ctx.bus.emit(
        ctx.task_id, "checkpoint", {"agent": ctx.agent, "id": sha[:12], "label": label}
    )
    return sha[:12]


async def _create_checkpoint(ctx: ToolContext, args: CreateCheckpointInput) -> dict[str, Any]:
    return {"checkpoint_id": await create_checkpoint(ctx, args.label)}


async def _restore_checkpoint(ctx: ToolContext, args: RestoreCheckpointInput) -> dict[str, Any]:
    sandbox = _require_sandbox(ctx)
    if args.checkpoint_id not in ctx.checkpoints:
        raise SandboxToolError(
            "unknown_checkpoint",
            f"not a checkpoint of this task. Known: {sorted(ctx.checkpoints) or 'none'}",
        )
    await git.restore(sandbox, args.checkpoint_id)
    await ctx.bus.emit(
        ctx.task_id,
        "checkpoint_restored",
        {"agent": ctx.agent, "id": args.checkpoint_id,
         "label": ctx.checkpoints[args.checkpoint_id]},
    )  # fmt: skip
    return {"restored": args.checkpoint_id, "label": ctx.checkpoints[args.checkpoint_id]}


# --- registry ----------------------------------------------------------------------------

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("list_files", "List files in the workspace (dependency and cache folders are "
             "hidden).", ListFilesInput, P.FS_READ, _list_files),
        Tool("read_file", "Read a text file, optionally a line range.", ReadFileInput,
             P.FS_READ, _read_file),
        Tool("search_code", "Search file contents for text or a regular expression.",
             SearchCodeInput, P.FS_READ, _search_code),
        Tool("write_file", "Create a file or replace its whole content. Parent folders are "
             "created.", WriteFileInput, P.FS_WRITE, _write_file),
        Tool("edit_file", "Replace an exact piece of text in an existing file. Prefer this to "
             "write_file for small changes.", EditFileInput, P.FS_WRITE, _edit_file),
        Tool("delete_file", "Delete a file or directory.", DeleteFileInput, P.FS_WRITE,
             _delete_file),
        Tool("execute_command", "Run one shell command in the workspace. No network. Use "
             "install_dependency for packages and run_tests for tests.", ExecuteCommandInput,
             P.EXEC, _execute_command, timeout_seconds=660),
        Tool("install_dependency", "Install packages with pip or npm. This is the only tool "
             "with network access, and it reaches package registries only.",
             InstallDependencyInput, P.INSTALL, _install_dependency, timeout_seconds=660),
        Tool("run_tests", "Run the project's tests and get pass/fail with the output.",
             RunTestsInput, P.EXEC, _run_tests, timeout_seconds=660),
        Tool("get_execution_logs", "Get more of the output of an earlier command.",
             GetExecutionLogsInput, P.LOGS, _get_execution_logs),
        Tool("inspect_git_diff", "Show what has changed in the workspace since the task "
             "started.", InspectGitDiffInput, P.DIFF, _inspect_git_diff, timeout_seconds=90),
        Tool("create_checkpoint", "Save the current workspace state so it can be restored.",
             CreateCheckpointInput, P.CHECKPOINT, _create_checkpoint, timeout_seconds=90),
        Tool("restore_checkpoint", "Roll the workspace back to a checkpoint, discarding "
             "everything since.", RestoreCheckpointInput, P.CHECKPOINT, _restore_checkpoint,
             timeout_seconds=90),
    ]
}  # fmt: skip


def tools_for(role: str) -> list[Tool]:
    """The tools a role may call, in registry order."""
    granted = ROLE_PERMISSIONS[role]
    return [t for t in TOOLS.values() if t.permission in granted]


def _summarise_args(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments as stored in the event log: long strings are cut, not kept whole."""
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 300:
            summary[key] = f"{value[:300]}… [{len(value)} characters]"
        else:
            summary[key] = value
    return summary


async def invoke(ctx: ToolContext, name: str, raw_args: dict[str, Any]) -> ToolResult:
    """Run one tool call for the agent in `ctx`. See the module docstring for the steps."""
    started = time.monotonic()
    tool = TOOLS.get(name)
    result: ToolResult

    if ctx.cancel.is_set():
        raise TaskCancelled()
    if tool is None:
        result = ToolResult(False, error={"code": "unknown_tool", "message": f"no tool {name!r}"})
    elif tool.permission not in ROLE_PERMISSIONS.get(ctx.agent, frozenset()):
        result = ToolResult(
            False,
            error={
                "code": "permission_denied",
                "message": f"the {ctx.agent} agent is not permitted to call {name}",
            },
        )
    else:
        try:
            args = tool.input_model.model_validate(raw_args)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
            result = ToolResult(False, error={"code": "invalid_input", "message": problems})
        else:
            ctx.budget.charge_tool(ctx.agent)
            try:
                data = await asyncio.wait_for(tool.handler(ctx, args), timeout=tool.timeout_seconds)
                result = ToolResult(True, data=data)
            except SandboxToolError as exc:
                result = ToolResult(False, error={"code": exc.code, "message": exc.message})
            except TimeoutError:
                result = ToolResult(
                    False,
                    error={"code": "timeout", "message": f"{name} exceeded its time limit"},
                )

    duration_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "tool_call", task_id=ctx.task_id, agent=ctx.agent, tool=name, ok=result.ok,
        error=result.error["code"] if result.error else None, duration_ms=duration_ms,
    )  # fmt: skip
    await ctx.bus.emit(
        ctx.task_id,
        "tool_call",
        {
            "agent": ctx.agent,
            "tool": name,
            "args": _summarise_args(raw_args if isinstance(raw_args, dict) else {}),
            "ok": result.ok,
            "error": result.error,
            "duration_ms": duration_ms,
        },
    )
    return result
