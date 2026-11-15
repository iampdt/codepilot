"""The orchestrator: one explicit state machine per task.

Each state has one handler. A handler does its work (usually: build a brief, run one agent,
store its contract) and returns the next state; `_transition` checks that move against the
table in `state.py` and writes it with a compare-and-set, so a task can never be in a state
the table doesn't allow, and an API action (cancel) can't be overwritten by a worker.

The orchestrator trusts nothing an agent reports about success. Whether tests pass is decided
in VERIFYING by running the test command itself and reading the exit code.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import roles
from app.agents.base import AgentFailed, run_agent
from app.agents.contracts import (
    CodingReport,
    DebugReport,
    ExecutionReport,
    Plan,
    ReviewReport,
)
from app.config import Settings
from app.events import EventBus
from app.llm.base import LLMError
from app.llm.router import ModelRouter
from app.logging import get_logger
from app.models import Artifact, Execution, Task, now
from app.orchestrator.budget import Budget, BudgetExceeded, BudgetLimits
from app.orchestrator.state import ACTIVE, TERMINAL, InvalidTransition, TaskState, assert_transition
from app.sandbox import git
from app.sandbox.archive import ArchiveWorkspace, zip_to_tar
from app.sandbox.base import ExecResult, Sandbox, SandboxDriver, SandboxError, SandboxLimits
from app.sandbox.policy import check_command, is_test_command
from app.tools.registry import (
    QuotaExceeded,
    TaskCancelled,
    ToolContext,
    create_checkpoint,
    format_failure,
    run_in_sandbox,
    run_tests,
)

log = get_logger("orchestrator")
S = TaskState

DEFAULT_TEST_COMMAND = "pytest -q"
STATS_INTERVAL_SECONDS = 10
# requirements.txt, requirements-dev.txt, dev-requirements.txt and the like.
MANIFEST_PIP = re.compile(r"^[\w.-]*requirements[\w.-]*\.txt$")


def cancel_key(task_id: str) -> str:
    return f"codepilot:cancel:{task_id}"


@dataclass
class TaskRun:
    """What one worker holds in memory while it drives a task."""

    task_id: str
    ctx: ToolContext
    sandbox: Sandbox | None = None
    base_archive: bytes | None = None
    background: list[asyncio.Task[None]] = field(default_factory=list)
    review_findings: list[dict[str, Any]] | None = None


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        bus: EventBus,
        router: ModelRouter,
        driver: SandboxDriver,
        redis: Redis | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.bus = bus
        self.router = router
        self.driver = driver
        self.redis = redis
        self.specs = roles.specs(settings)
        self._handlers = {
            S.QUEUED: self._queued,
            S.PLANNING: self._planning,
            S.PROVISIONING: self._provisioning,
            S.CODING: self._coding,
            S.EXECUTING: self._executing,
            S.VERIFYING: self._verifying,
            S.DEBUGGING: self._debugging,
            S.REVIEWING: self._reviewing,
        }

    # --- persistence helpers -----------------------------------------------------------------

    async def _load(self, task_id: str) -> Task:
        async with self.sessions() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task {task_id} not found")
            return task

    async def _save(self, task_id: str, **values: Any) -> None:
        async with self.sessions() as session:
            await session.execute(update(Task).where(Task.id == task_id).values(**values))
            await session.commit()

    async def _transition(
        self, run: TaskRun, current: TaskState, target: TaskState, **values: Any
    ) -> TaskState:
        assert_transition(current, target)
        if target in TERMINAL:
            # Export the result and remove the sandbox first, so that a task which reads as
            # finished always has its archive, diff and changed files in place.
            if run.sandbox is not None:
                await self._finalise(run)
            values["finished_at"] = now()
        values["usage"] = run.ctx.budget.snapshot()
        async with self.sessions() as session:
            # Compare-and-set: if the API moved the task (a cancel), this update matches nothing.
            result = await session.execute(
                update(Task)
                .where(Task.id == run.task_id, Task.state == current.value)
                .values(state=target.value, **values)
            )
            await session.commit()
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise TaskCancelled()
        await self.bus.emit(
            run.task_id,
            "state_changed",
            {"from": current.value, "to": target.value, "error": values.get("error")},
        )
        log.info("state_changed", task_id=run.task_id, old=current.value, new=target.value)
        return target

    async def _store_report(
        self, task_id: str, key: str, report: Any, append: bool = False
    ) -> None:
        async with self.sessions() as session:
            task = await session.get(Task, task_id, with_for_update=True)
            assert task is not None
            reports = dict(task.reports or {})
            if append:
                reports[key] = [*reports.get(key, []), report]
            else:
                reports[key] = report
            task.reports = reports
            await session.commit()

    # --- the run loop ------------------------------------------------------------------------

    async def run(self, task_id: str) -> None:
        """Drive a task from its stored state until it waits for the user or ends."""
        task = await self._load(task_id)
        state = S(task.state)
        if state in TERMINAL or state == S.AWAITING_APPROVAL:
            return
        budget = Budget.restore(
            BudgetLimits(
                self.settings.max_llm_calls,
                self.settings.max_total_tokens,
                self.settings.max_tool_calls,
                self.settings.max_wall_seconds,
            ),
            task.usage,
        )
        ctx = ToolContext(
            task_id=task_id,
            agent="orchestrator",
            workspace=ArchiveWorkspace(None),
            bus=self.bus,
            budget=budget,
            settings=self.settings,
            sessions=self.sessions,
            cancel=asyncio.Event(),
        )
        run = TaskRun(task_id=task_id, ctx=ctx)
        run.background.append(asyncio.create_task(self._watch_cancel(run)))

        # A worker only ever starts a task at one of these states. Anything else means an
        # earlier worker died mid-run and its sandbox is gone: fail cleanly rather than guess.
        if state not in (S.QUEUED, S.PLANNING, S.PROVISIONING):
            await self._fail(run, state, "the worker running this task stopped unexpectedly")
            await self._stop_background(run)
            return
        if task.started_at is None:
            await self._save(task_id, started_at=now())

        try:
            waiting: frozenset[TaskState] = TERMINAL | {S.AWAITING_APPROVAL}
            while state not in waiting:
                if ctx.cancel.is_set():
                    raise TaskCancelled()
                ctx.for_agent("orchestrator")
                state = await self._handlers[state](run, state)
        except TaskCancelled:
            await self._cancelled(run)
        except BudgetExceeded as exc:
            await self._fail(run, state, str(exc))
        except (AgentFailed, LLMError, SandboxError, QuotaExceeded, InvalidTransition) as exc:
            await self._fail(run, state, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a task must always reach a terminal state
            log.exception("task_crashed", task_id=task_id)
            await self._fail(run, state, f"internal error: {type(exc).__name__}: {exc}")
        finally:
            await self._stop_background(run)
            if run.sandbox is not None:
                await self._finalise(run)

    async def _current_state(self, task_id: str) -> TaskState:
        return S((await self._load(task_id)).state)

    async def _fail(self, run: TaskRun, _state: TaskState, error: str) -> None:
        current = await self._current_state(run.task_id)
        if current in TERMINAL:
            return
        with contextlib.suppress(TaskCancelled, InvalidTransition):
            await self._transition(run, current, S.FAILED, error=error[:2000])

    async def _cancelled(self, run: TaskRun) -> None:
        current = await self._current_state(run.task_id)
        if current in TERMINAL:
            await self._save(run.task_id, usage=run.ctx.budget.snapshot())
            return
        with contextlib.suppress(TaskCancelled, InvalidTransition):
            await self._transition(run, current, S.CANCELLED, error="cancelled by the user")

    async def _watch_cancel(self, run: TaskRun) -> None:
        """Turn the API's cancel flag into an event every loop and command can wait on."""
        if self.redis is None:
            return
        while not run.ctx.cancel.is_set():
            try:
                if await self.redis.exists(cancel_key(run.task_id)):
                    run.ctx.cancel.set()
                    return
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel_poll_failed", error=str(exc))
            await asyncio.sleep(1.0)

    async def _watch_stats(self, run: TaskRun) -> None:
        while run.sandbox is not None:
            await asyncio.sleep(STATS_INTERVAL_SECONDS)
            with contextlib.suppress(Exception):
                stats = await run.sandbox.stats()
                if stats:
                    await self.bus.emit(run.task_id, "sandbox_stats", stats)

    async def _stop_background(self, run: TaskRun) -> None:
        for job in run.background:
            job.cancel()
        for job in run.background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job
        run.background.clear()

    # --- state handlers ----------------------------------------------------------------------

    async def _queued(self, run: TaskRun, state: TaskState) -> TaskState:
        return await self._transition(run, state, S.PLANNING)

    async def _base_archive(self, task: Task) -> bytes | None:
        if task.base_artifact_id is None:
            return None
        async with self.sessions() as session:
            artifact = await session.get(Artifact, task.base_artifact_id)
            return artifact.data if artifact else None

    async def _planning(self, run: TaskRun, state: TaskState) -> TaskState:
        task = await self._load(run.task_id)
        if task.plan_revisions > self.settings.max_plan_revisions:
            return await self._transition(run, state, S.FAILED, error="plan revision limit reached")
        archive = ArchiveWorkspace(await self._base_archive(task))
        run.ctx.workspace = archive
        files = sorted(archive.files)
        plan = await run_agent(
            self.specs["planner"],
            brief=roles.planner_brief(
                task.description, files, task.plan if task.plan_feedback else None,
                task.plan_feedback,
            ),
            ctx=run.ctx,
            router=self.router,
            output_model=Plan,
        )  # fmt: skip
        plan_data = plan.model_dump()
        await self.bus.emit(run.task_id, "plan_ready", {"plan": plan_data})
        return await self._transition(
            run, state, S.AWAITING_APPROVAL, plan=plan_data, plan_feedback=None
        )

    async def _provisioning(self, run: TaskRun, state: TaskState) -> TaskState:
        task = await self._load(run.task_id)
        limits = SandboxLimits(
            memory_mb=self.settings.sandbox_memory_mb,
            cpus=self.settings.sandbox_cpus,
            pids=self.settings.sandbox_pids,
            tmp_mb=self.settings.sandbox_tmp_mb,
            workspace_mb=self.settings.sandbox_workspace_mb,
            file_mb=self.settings.sandbox_file_mb,
        )
        sandbox = await self.driver.create(run.task_id, limits)
        run.sandbox = sandbox
        run.ctx.sandbox = sandbox
        run.ctx.workspace = sandbox
        archive = await self._base_archive(task)
        if archive:
            await sandbox.unpack(zip_to_tar(archive))
        run.ctx.base_commit = await git.init_repo(sandbox)
        isolation = {
            "driver": sandbox.isolation.driver,
            "layers": sandbox.isolation.layers,
            "limits": sandbox.isolation.limits,
        }
        await self.bus.emit(run.task_id, "sandbox_ready", isolation)
        run.background.append(asyncio.create_task(self._watch_stats(run)))
        return await self._transition(run, state, S.CODING, isolation=isolation)

    def _usable_test_command(self, *candidates: str | None) -> str:
        """The first candidate that is a real, allowed test-runner command."""
        for command in candidates:
            if command and is_test_command(command) and check_command(command).allowed:
                return command
        return DEFAULT_TEST_COMMAND

    async def _workspace_files(self, run: TaskRun) -> list[str]:
        listing = await run.ctx.workspace.file_op("list_files", {"path": ".", "max_entries": 400})
        return [e["path"] for e in listing["entries"] if e["type"] == "file"]

    async def _coding(self, run: TaskRun, state: TaskState) -> TaskState:
        task = await self._load(run.task_id)
        assert task.plan is not None
        report = await run_agent(
            self.specs["coder"],
            brief=roles.coder_brief(
                task.description, task.plan, await self._workspace_files(run), run.review_findings
            ),
            ctx=run.ctx,
            router=self.router,
            output_model=CodingReport,
        )
        run.ctx.for_agent("orchestrator")
        label = "after-coding" if task.review_rounds == 0 else "after-revision"
        await create_checkpoint(run.ctx, label)
        await self._store_report(run.task_id, "coding", report.model_dump())
        test_command = self._usable_test_command(report.test_command, task.plan.get("test_command"))
        run.ctx.test_command = test_command
        return await self._transition(run, state, S.EXECUTING, test_command=test_command)

    async def _executing(self, run: TaskRun, state: TaskState) -> TaskState:
        task = await self._load(run.task_id)
        assert task.plan is not None
        run.ctx.test_command = task.test_command or DEFAULT_TEST_COMMAND
        report = await run_agent(
            self.specs["executor"],
            brief=roles.executor_brief(
                task.plan, (task.reports or {}).get("coding", {}), run.ctx.test_command
            ),
            ctx=run.ctx,
            router=self.router,
            output_model=ExecutionReport,
        )
        await self._store_report(run.task_id, "execution", report.model_dump())
        test_command = self._usable_test_command(report.test_command, task.test_command)
        run.ctx.test_command = test_command
        return await self._transition(run, state, S.VERIFYING, test_command=test_command)

    async def _clean_install(self, run: TaskRun) -> tuple[ExecResult, int] | None:
        """Throw away every package the agents installed and install again from the project's
        own manifests. Tests that pass after this pass for anyone who downloads the result;
        a package an agent installed by hand but never declared makes them fail here."""
        assert run.sandbox is not None
        files = await self._workspace_files(run)
        commands: list[str] = []
        for path in files:
            directory, _, name = path.rpartition("/")
            if path.count("/") > 1:
                continue
            prefix = f"cd {shlex.quote(directory)} && " if directory else ""
            if MANIFEST_PIP.match(name):
                commands.append(f"{prefix}pip install --no-input -r {shlex.quote(name)}")
            elif name == "package.json":
                commands.append(f"{prefix}rm -rf node_modules && npm install --no-audit --no-fund")
        if not commands:
            return None
        await run.sandbox.exec("rm -rf /home/sandbox/.local /home/sandbox/.npm", timeout=60)
        outcome: tuple[ExecResult, int] | None = None
        for command in commands:
            outcome = await run_in_sandbox(
                run.ctx,
                command,
                kind="install",
                timeout=self.settings.install_timeout_seconds,
                network=True,
            )
            if not outcome[0].ok:
                break
        return outcome

    async def _verifying(self, run: TaskRun, state: TaskState) -> TaskState:
        """Run the tests ourselves. This result, not any agent's report, decides the outcome."""
        task = await self._load(run.task_id)
        command = self._usable_test_command(task.test_command)
        run.ctx.for_agent("orchestrator")
        install = await self._clean_install(run)
        if install is not None and not install[0].ok:
            # The project's own manifests don't install: that is a failure to debug.
            result: dict[str, Any] = {
                "passed": False,
                "exit_code": install[0].exit_code,
                "summary": {},
                "execution_id": install[1],
            }
        else:
            result = await run_tests(run.ctx, command, kind="verify")
        verification = {
            "command": command,
            "passed": result["passed"],
            "exit_code": result["exit_code"],
            "summary": result["summary"],
            "execution_id": result["execution_id"],
            "after_debug_iterations": task.debug_iterations,
            "clean_install": install is not None,
        }
        await self._store_report(run.task_id, "verification", verification)
        if result["passed"]:
            return await self._transition(run, state, S.REVIEWING, tests_passed=True)
        if task.debug_iterations < self.settings.max_debug_iterations:
            return await self._transition(run, state, S.DEBUGGING, tests_passed=False)
        # Out of debug attempts: the work is still reviewed and exported, but marked failing.
        return await self._transition(run, state, S.REVIEWING, tests_passed=False)

    async def _debugging(self, run: TaskRun, state: TaskState) -> TaskState:
        task = await self._load(run.task_id)
        assert task.plan is not None
        attempt = task.debug_iterations + 1
        await self._save(run.task_id, debug_iterations=attempt)
        run.ctx.for_agent("orchestrator")
        await create_checkpoint(run.ctx, f"before-debug-{attempt}")
        verification = (task.reports or {}).get("verification", {})
        async with self.sessions() as session:
            execution = await session.get(Execution, verification.get("execution_id"))
        failure_output = format_failure(execution.output if execution else "")
        run.ctx.test_command = task.test_command or DEFAULT_TEST_COMMAND
        report = await run_agent(
            self.specs["debugger"],
            brief=roles.debugger_brief(
                task.description,
                task.plan,
                run.ctx.test_command,
                failure_output,
                attempt,
                self.settings.max_debug_iterations,
                (task.reports or {}).get("debugging", []),
            ),
            ctx=run.ctx,
            router=self.router,
            output_model=DebugReport,
        )
        await self._store_report(
            run.task_id, "debugging", {"attempt": attempt, **report.model_dump()}, append=True
        )
        return await self._transition(run, state, S.VERIFYING)

    async def _reviewing(self, run: TaskRun, state: TaskState) -> TaskState:
        task = await self._load(run.task_id)
        assert task.plan is not None and run.sandbox is not None and run.ctx.base_commit
        diff_stat = await git.diff(run.sandbox, run.ctx.base_commit, stat=True)
        report = await run_agent(
            self.specs["reviewer"],
            brief=roles.reviewer_brief(
                task.description, task.plan, (task.reports or {}).get("verification"), diff_stat
            ),
            ctx=run.ctx,
            router=self.router,
            output_model=ReviewReport,
        )
        await self._store_report(run.task_id, "review", report.model_dump())
        await self.bus.emit(run.task_id, "review_ready", {"review": report.model_dump()})
        blocking = report.blocking
        if (
            report.verdict == "request_changes"
            and blocking
            and task.review_rounds < self.settings.max_review_rounds
        ):
            run.review_findings = [f.model_dump() for f in blocking]
            return await self._transition(
                run, state, S.CODING, review_rounds=task.review_rounds + 1
            )
        if task.tests_passed:
            return await self._transition(run, state, S.COMPLETED)
        return await self._transition(
            run,
            state,
            S.FAILED,
            error=f"tests still failing after {task.debug_iterations} debugging attempt(s)",
        )

    # --- finishing ---------------------------------------------------------------------------

    async def _finalise(self, run: TaskRun) -> None:
        """Keep what the sandbox produced (diff, changed files, archive), then destroy it.

        Runs for every outcome once a sandbox exists, so a failed or cancelled task still
        leaves its work behind for inspection."""
        sandbox = run.sandbox
        assert sandbox is not None
        try:
            if run.ctx.base_commit:
                task = await self._load(run.task_id)
                changes = await git.changes(sandbox, run.ctx.base_commit)
                final_diff = await git.diff(sandbox, run.ctx.base_commit)
                data = await sandbox.pack()
                async with self.sessions() as session:
                    artifact = Artifact(
                        project_id=task.project_id,
                        task_id=task.id,
                        kind="result",
                        filename=f"codepilot-{task.id}.zip",
                        size=len(data),
                        data=data,
                    )
                    session.add(artifact)
                    await session.commit()
                    artifact_id = artifact.id
                await self._save(
                    run.task_id,
                    changes=changes,
                    final_diff=final_diff,
                    result_artifact_id=artifact_id,
                    usage=run.ctx.budget.snapshot(),
                )
                await self.bus.emit(
                    run.task_id,
                    "result_ready",
                    {"artifact_id": artifact_id, "bytes": len(data), "changes": changes},
                )
        except Exception as exc:  # noqa: BLE001 - cleanup must still happen
            log.exception("finalise_failed", task_id=run.task_id)
            await self.bus.emit(run.task_id, "error", {"message": f"could not export: {exc}"})
        finally:
            run.sandbox = None
            with contextlib.suppress(Exception):
                await sandbox.destroy()
            await self.bus.emit(run.task_id, "sandbox_destroyed", {})

    async def recover(self, is_locked: Callable[[str], Awaitable[bool]]) -> tuple[int, list[str]]:
        """At worker start: deal with tasks a dead worker left in an active state.

        A task whose lock is still held belongs to a live worker and is left alone. One that
        was planning or provisioning has lost nothing and is handed back to be queued again.
        Anything later has lost its sandbox, so it is failed. Returns (failed, to requeue)."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(Task).where(Task.state.in_([s.value for s in ACTIVE | {S.QUEUED}]))
                )
            ).scalars()
            stale = [(t.id, S(t.state)) for t in rows]
        resumable = {S.QUEUED, S.PLANNING, S.PROVISIONING}
        failed = 0
        requeue: list[str] = []
        alive: set[str] = set()
        for task_id, state in stale:
            if await is_locked(task_id):
                alive.add(task_id)
                continue
            if state in resumable:
                requeue.append(task_id)
                continue
            async with self.sessions() as session:
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.state == state.value)
                    .values(
                        state=S.FAILED.value,
                        error="the worker running this task stopped unexpectedly",
                        finished_at=now(),
                    )
                )
                await session.commit()
            await self.bus.emit(
                task_id, "state_changed", {"from": state.value, "to": S.FAILED.value}
            )
            failed += 1
        with contextlib.suppress(Exception):
            await self.driver.cleanup(alive)
        return failed, requeue
