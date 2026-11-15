"""The sandbox contract. The orchestrator and the tools only ever see these types, so the
isolation mechanism can change (runc, gVisor, a microVM) without touching agent code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

OutputCallback = Callable[[str], Awaitable[None]]


class SandboxError(Exception):
    """The sandbox itself failed (daemon unreachable, container gone)."""


class SandboxToolError(Exception):
    """A file operation was refused or failed; safe to show to the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SandboxLimits:
    memory_mb: int
    cpus: float
    pids: int
    tmp_mb: int
    workspace_mb: int
    file_mb: int


@dataclass
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool
    duration_ms: int
    truncated: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


@dataclass
class IsolationReport:
    """What actually isolates this sandbox, as started. Shown in the UI and stored on the task."""

    driver: str
    layers: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)


class Workspace(Protocol):
    """Read access to a codebase. An archive satisfies this without running anything."""

    async def file_op(self, op: str, args: dict[str, Any]) -> dict[str, Any]: ...


class Sandbox(Workspace, Protocol):
    id: str
    isolation: IsolationReport

    async def exec(
        self,
        command: str,
        *,
        timeout: int,
        network: bool = False,
        env: dict[str, str] | None = None,
        on_output: OutputCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> ExecResult: ...

    async def unpack(self, tar_bytes: bytes) -> dict[str, Any]: ...

    async def pack(self) -> bytes: ...

    async def stats(self) -> dict[str, Any]: ...

    async def destroy(self) -> None: ...


class SandboxDriver(Protocol):
    name: str

    async def create(self, task_id: str, limits: SandboxLimits) -> Sandbox: ...

    async def attach(self, task_id: str) -> Sandbox | None:
        """A handle to a task's running sandbox, for read-only use by the API; None if gone."""
        ...

    async def cleanup(self, keep_task_ids: set[str]) -> int:
        """Remove sandboxes that belong to no live task. Returns how many were removed."""
        ...
