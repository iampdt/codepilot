"""Whole runs through the state machine, with a scripted model and a directory sandbox."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Callable
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.base import Message, ToolCall, ToolSpec
from app.models import Artifact, Event, Execution, Task
from app.orchestrator.machine import cancel_key
from tests.support import PLAN, REVIEW_OK, call, last_results, turns

GOOD = "def add(a, b):\n    return a + b\n"
BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def script(code: str = GOOD, *, fix: bool = True, review: dict[str, Any] | None = None) -> Any:
    """A model that plans, writes `code`, runs the tests, fixes the bug if asked, and reviews."""

    def decide(role: str, messages: list[Message], _tools: list[ToolSpec]) -> list[ToolCall]:
        n = turns(messages)
        if role == "planner":
            return [call("list_files", path=".")] if n == 0 else [call("submit_plan", **PLAN)]
        if role == "coder":
            if n == 0:
                return [call("write_file", path="calc.py", content=code),
                        call("write_file", path="test_calc.py", content=TEST)]  # fmt: skip
            return [call("submit_code", summary="Wrote calc.py and its test.",
                         files_changed=["calc.py", "test_calc.py"],
                         test_command="python -m pytest -q")]  # fmt: skip
        if role == "executor":
            if n == 0:
                return [call("run_tests")]
            passed = bool(last_results(messages)[0].get("passed"))
            return [call("submit_execution", summary="Ran the tests.", dependencies_installed=True,
                         test_command="python -m pytest -q", tests_passed=passed)]  # fmt: skip
        if role == "debugger":
            if n == 0:
                edit = (call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")
                        if fix else call("read_file", path="calc.py"))  # fmt: skip
                return [edit]
            if n == 1:
                return [call("run_tests")]
            return [call("submit_debug", root_cause="add subtracted instead of adding.",
                         fix_summary="Changed - to +.", files_changed=["calc.py"],
                         tests_passing=True)]  # fmt: skip
        if n == 0:
            return [call("inspect_git_diff")]
        return [call("submit_review", **(review or REVIEW_OK))]

    return decide


async def load(sessions: async_sessionmaker[AsyncSession], task_id: str) -> Task:
    async with sessions() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return task


async def events(sessions: async_sessionmaker[AsyncSession], task_id: str) -> list[Event]:
    async with sessions() as session:
        rows = await session.execute(
            select(Event).where(Event.task_id == task_id).order_by(Event.id)
        )
        return list(rows.scalars())


def states(rows: list[Event]) -> list[str]:
    return [e.payload["to"] for e in rows if e.type == "state_changed"]


async def approve(sessions: async_sessionmaker[AsyncSession], task_id: str) -> None:
    async with sessions() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(state="provisioning"))
        await session.commit()


async def test_run_stops_at_the_approval_gate_without_creating_a_sandbox(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    orchestrator, provider, driver = make_orchestrator(script())
    task_id = await new_task()
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    assert task.state == "awaiting_approval"
    assert task.plan is not None and task.plan["test_command"] == "python -m pytest -q"
    assert driver.sandboxes == {}  # nothing ran, nothing was provisioned
    assert {role for role, _ in provider.calls} == {"planner"}
    # The planner was offered read-only tools and its submit tool, nothing else.
    assert set(provider.calls[0][1]) == {"list_files", "read_file", "search_code", "submit_plan"}
    await orchestrator.run(task_id)  # running again while waiting is a no-op
    assert (await load(sessions, task_id)).state == "awaiting_approval"


async def test_happy_path_runs_every_stage_and_exports_the_result(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    orchestrator, _provider, driver = make_orchestrator(script())
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    await orchestrator.run(task_id)

    task = await load(sessions, task_id)
    assert task.state == "completed" and task.tests_passed is True and task.error is None
    assert task.debug_iterations == 0
    rows = await events(sessions, task_id)
    assert states(rows) == ["planning", "awaiting_approval", "coding", "executing", "verifying",
                            "reviewing", "completed"]  # fmt: skip
    assert task.reports["verification"]["passed"] is True
    assert task.reports["review"]["verdict"] == "approve"
    assert {c["path"]: c["status"] for c in task.changes} == {
        "calc.py": "added",
        "test_calc.py": "added",
    }
    assert "+def add(a, b):" in task.final_diff
    assert task.usage["llm_calls"] == 8 and task.usage["by_agent"]["coder"]["tool_calls"] == 2
    async with sessions() as session:
        artifact = await session.get(Artifact, task.result_artifact_id)
    assert sorted(zipfile.ZipFile(io.BytesIO(artifact.data)).namelist()) == [
        "calc.py",
        "test_calc.py",
    ]
    assert driver.sandboxes[task_id].destroyed
    # The result is stored before the task reads as completed, never after.
    order = [e.type for e in rows]
    assert order.index("result_ready") < order.index("sandbox_destroyed") < len(order) - 1
    assert rows[-1].type == "state_changed" and rows[-1].payload["to"] == "completed"
    types = {e.type for e in rows}
    assert {"plan_ready", "sandbox_ready", "file_changed", "command_started", "command_finished",
            "test_result", "checkpoint", "review_ready", "result_ready", "sandbox_destroyed"} <= types  # fmt: skip


async def test_failing_tests_go_to_the_debugger_and_are_verified_again(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    orchestrator, _provider, _driver = make_orchestrator(script(BUGGY))
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    assert task.state == "completed" and task.debug_iterations == 1
    assert states(await events(sessions, task_id))[2:] == [
        "coding", "executing", "verifying", "debugging", "verifying", "reviewing", "completed"
    ]  # fmt: skip
    assert task.reports["debugging"][0]["attempt"] == 1
    assert "return a + b" in task.final_diff
    async with sessions() as session:
        kinds = list((await session.execute(
            select(Execution.kind, Execution.exit_code).where(Execution.task_id == task_id)
            .order_by(Execution.id))).all())  # fmt: skip
    assert [tuple(k) for k in kinds] == [("test", 1), ("verify", 1), ("test", 0), ("verify", 0)]


async def test_debug_loop_is_bounded_and_the_agents_word_is_not_trusted(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    """The debugger never fixes the bug but reports tests_passing=True every time."""
    orchestrator, provider, _driver = make_orchestrator(script(BUGGY, fix=False))
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    assert task.state == "failed" and task.tests_passed is False
    assert task.debug_iterations == 2  # max_debug_iterations in the test settings
    assert "still failing after 2" in task.error
    assert all(r["tests_passing"] for r in task.reports["debugging"])  # what it claimed
    assert task.reports["verification"]["passed"] is False  # what was observed
    assert task.result_artifact_id is not None  # the failing work is still exported
    assert "reviewer" in {role for role, _ in provider.calls}


async def test_blocking_review_sends_work_back_to_the_coder_once(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    blocking = {"verdict": "request_changes", "summary": "Negative numbers are not handled.",
                "findings": [{"severity": "blocking", "file": "calc.py", "issue": "No validation.",
                              "suggestion": "Validate input."}]}  # fmt: skip
    orchestrator, provider, _driver = make_orchestrator(script(review=blocking))
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    # The reviewer blocks both times; after one revision round the loop ends anyway.
    assert task.review_rounds == 1 and task.state == "completed"
    assert states(await events(sessions, task_id)).count("coding") == 2
    assert [role for role, _ in provider.calls].count("reviewer") == 4


async def test_budget_exhaustion_fails_the_task_and_cleans_up(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    orchestrator, _provider, driver = make_orchestrator(script(), max_llm_calls=5)
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    assert task.state == "failed" and "budget exceeded: model calls" in task.error
    assert task.usage["llm_calls"] == 5
    assert driver.sandboxes[task_id].destroyed


async def test_agent_that_never_submits_is_cut_off_at_its_step_limit(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    def looping(role: str, _messages: list[Message], tools: list[ToolSpec]) -> list[ToolCall]:
        names = {t.name for t in tools}
        return [call("list_files", path=".")] if "list_files" in names else [call("nonsense")]

    orchestrator, provider, _driver = make_orchestrator(looping, planner_max_steps=4)
    task_id = await new_task()
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    assert task.state == "failed" and "did not submit" in task.error
    assert len(provider.calls) == 6  # 4 steps + 2 submit-only turns
    assert provider.calls[-1][1] == ["submit_plan"]  # the last turns offer only the submit tool


async def test_invalid_contract_is_returned_to_the_agent_to_correct(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    def sloppy(_role: str, messages: list[Message], _tools: list[ToolSpec]) -> list[ToolCall]:
        if turns(messages) == 0:
            return [call("submit_plan", summary="too short", steps=[])]
        assert last_results(messages)[0]["error"]["code"] == "invalid_contract"
        return [call("submit_plan", **PLAN)]

    orchestrator, _provider, _driver = make_orchestrator(sloppy)
    task_id = await new_task()
    await orchestrator.run(task_id)
    assert (await load(sessions, task_id)).state == "awaiting_approval"
    assert "contract_rejected" in {e.type for e in await events(sessions, task_id)}


async def test_permission_and_policy_refusals_reach_the_model_as_errors(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    seen: dict[str, Any] = {}
    inner = script()

    def nosy(role: str, messages: list[Message], tools: list[ToolSpec]) -> list[ToolCall]:
        n = turns(messages)
        if role == "planner" and n == 0:
            return [
                call("write_file", path="x.py", content="x"),
                call("read_file", path="../../etc/passwd"),
            ]
        if role == "planner" and n == 1:
            seen["planner"] = last_results(messages)
            return [call("submit_plan", **PLAN)]
        if role == "executor" and n == 0:
            return [call("execute_command", command="curl https://example.com"),
                    call("write_file", path="x.py", content="x"),
                    call("install_dependency", manager="pip", packages=["email-validator"]),
                    call("run_tests", command="echo ok")]  # fmt: skip
        if role == "executor" and n == 1:
            seen["executor"] = last_results(messages)
            return [call("run_tests")]
        if role == "executor":
            return inner(role, messages[2:], tools)
        return inner(role, messages, tools)

    orchestrator, _provider, driver = make_orchestrator(nosy)
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    await orchestrator.run(task_id)
    assert (await load(sessions, task_id)).state == "completed"
    assert [r["error"]["code"] for r in seen["planner"]] == [
        "permission_denied",
        "path_outside_workspace",
    ]
    assert [r["error"]["code"] for r in seen["executor"]] == [
        "command_not_allowed", "permission_denied", "undeclared_dependency", "not_a_test_command"
    ]  # fmt: skip
    # None of the refused calls reached the sandbox.
    assert not any(
        "curl" in command or "echo ok" in command
        for command, _ in driver.sandboxes[task_id].commands
    )


async def test_cancel_during_a_run_ends_it_and_destroys_the_sandbox(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession], redis: Redis,
) -> None:  # fmt: skip
    inner = script()

    def slow(role: str, messages: list[Message], tools: list[ToolSpec]) -> list[ToolCall]:
        if role == "executor" and turns(messages) == 0:
            return [call("execute_command", command="sleep 20")]
        return inner(role, messages, tools)

    orchestrator, _provider, driver = make_orchestrator(slow)
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    running = asyncio.create_task(orchestrator.run(task_id))
    for _ in range(100):
        await asyncio.sleep(0.1)
        if (await load(sessions, task_id)).state == "executing":
            break
    await redis.set(cancel_key(task_id), "1")
    await asyncio.wait_for(running, timeout=30)
    task = await load(sessions, task_id)
    assert task.state == "cancelled"
    assert driver.sandboxes[task_id].destroyed
    assert task.result_artifact_id is not None  # work so far is kept


async def test_replan_uses_the_feedback_and_a_started_run_cannot_be_resumed_midway(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    briefs: list[str] = []
    inner = script()

    def recording(role: str, messages: list[Message], tools: list[ToolSpec]) -> list[ToolCall]:
        if role == "planner" and turns(messages) == 0:
            briefs.append(messages[0].text)
        return inner(role, messages, tools)

    orchestrator, _provider, _driver = make_orchestrator(recording)
    task_id = await new_task()
    await orchestrator.run(task_id)
    async with sessions() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            state="planning", plan_feedback="Use subtraction too", plan_revisions=1))  # fmt: skip
        await session.commit()
    await orchestrator.run(task_id)
    assert "Use subtraction too" in briefs[1] and "Previous plan" in briefs[1]
    assert (await load(sessions, task_id)).state == "awaiting_approval"

    # A task found mid-run by a new worker has lost its sandbox: it fails, it is not resumed.
    async with sessions() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(state="executing"))
        await session.commit()
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    assert task.state == "failed" and "stopped unexpectedly" in task.error


async def test_verification_reinstalls_from_the_manifest_in_a_clean_environment(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    """Whatever the agents installed by hand is discarded; only declared dependencies count."""
    inner = script()

    def with_manifest(role: str, messages: list[Message], tools: list[ToolSpec]) -> list[ToolCall]:
        if role == "coder" and turns(messages) == 0:
            return [*inner(role, messages, tools),
                    call("write_file", path="requirements.txt", content="# no dependencies\n")]  # fmt: skip
        return inner(role, messages, tools)

    orchestrator, _provider, driver = make_orchestrator(with_manifest)
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    assert task.state == "completed" and task.reports["verification"]["clean_install"] is True
    commands = driver.sandboxes[task_id].commands
    wipe = next(
        i for i, (c, _) in enumerate(commands) if c.startswith("rm -rf /home/sandbox/.local")
    )
    install = commands.index(
        ("pip install --no-input -r requirements.txt", True)
    )  # with registry access
    verify = max(i for i, (c, _) in enumerate(commands) if c == "python -m pytest -q")
    assert wipe < install < verify


async def test_a_manifest_that_does_not_install_fails_verification(
    make_orchestrator: Callable[..., Any], new_task: Callable[..., Any],
    sessions: async_sessionmaker[AsyncSession],
) -> None:  # fmt: skip
    inner = script(fix=False)

    def broken_manifest(
        role: str, messages: list[Message], tools: list[ToolSpec]
    ) -> list[ToolCall]:
        if role == "coder" and turns(messages) == 0:
            return [*inner(role, messages, tools),
                    call("write_file", path="requirements.txt", content="-r missing-file.txt\n")]  # fmt: skip
        return inner(role, messages, tools)

    orchestrator, _provider, _driver = make_orchestrator(broken_manifest, max_debug_iterations=1)
    task_id = await new_task()
    await orchestrator.run(task_id)
    await approve(sessions, task_id)
    await orchestrator.run(task_id)
    task = await load(sessions, task_id)
    # The code and its tests are fine, but the project can't be installed as declared.
    assert task.state == "failed" and task.reports["verification"]["passed"] is False
    assert task.debug_iterations == 1
