"""HTTP API. Thin by design: routes validate, read or write task state, and enqueue work.
Nothing here runs an agent or a command; that is the worker's job."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RuntimeDep, SessionDep, require_token
from app.events import channel, serialise
from app.models import Artifact, Event, Execution, Project, Task, now
from app.orchestrator.machine import cancel_key
from app.orchestrator.state import ACTIVE, TERMINAL, TaskState
from app.runtime import Runtime
from app.sandbox import git
from app.sandbox.archive import ArchiveError, ArchiveWorkspace, validate_zip
from app.sandbox.base import SandboxError, SandboxToolError, Workspace
from app.worker import enqueue

S = TaskState
router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
public = APIRouter(prefix="/api")

# States in which the task's sandbox exists and holds the newest files.
SANDBOX_STATES = {S.CODING, S.EXECUTING, S.VERIFYING, S.DEBUGGING, S.REVIEWING}


# --- serialisers -------------------------------------------------------------------------


def project_json(project: Project, tasks: list[Task] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at.isoformat(),
    }
    if tasks is not None:
        data["tasks"] = [task_json(t, brief=True) for t in tasks]
    return data


def task_json(task: Task, *, brief: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": task.id,
        "project_id": task.project_id,
        "description": task.description,
        "state": task.state,
        "error": task.error,
        "tests_passed": task.tests_passed,
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }
    if brief:
        return data
    data.update(
        plan=task.plan,
        plan_revisions=task.plan_revisions,
        reports=task.reports or {},
        usage=task.usage or {},
        debug_iterations=task.debug_iterations,
        review_rounds=task.review_rounds,
        test_command=task.test_command,
        changes=task.changes,
        isolation=task.isolation,
        has_result=task.result_artifact_id is not None,
    )
    return data


async def _task(session: AsyncSession, task_id: str) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


# --- health ------------------------------------------------------------------------------


@public.get("/health")
async def health(runtime: RuntimeDep) -> dict[str, Any]:
    return {
        "status": "ok",
        "sandbox": runtime.driver.name,
        "llm": {
            "provider": runtime.settings.llm_provider,
            "models": runtime.settings.model_chain,
            "configured": runtime.llm_configured,
        },
        "auth_required": bool(runtime.settings.api_token),
    }


# --- projects ----------------------------------------------------------------------------


@router.post("/projects", status_code=201)
async def create_project(
    runtime: RuntimeDep,
    session: SessionDep,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    description: Annotated[str, Form(max_length=2000)] = "",
    archive: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    """Create a project, empty or from an uploaded zip of an existing codebase."""
    project = Project(name=name.strip(), description=description.strip())
    session.add(project)
    await session.flush()
    if archive is not None:
        limit = runtime.settings.upload_max_mb * 1024 * 1024
        data = await archive.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"archive is over {runtime.settings.upload_max_mb} MB")
        try:
            validate_zip(data)
        except ArchiveError as exc:
            raise HTTPException(422, str(exc)) from exc
        session.add(
            Artifact(
                project_id=project.id,
                kind="source",
                filename=(archive.filename or "upload.zip")[:200],
                size=len(data),
                data=data,
            )
        )
    await session.commit()
    return project_json(project, [])


@router.get("/projects")
async def list_projects(session: SessionDep) -> list[dict[str, Any]]:
    projects = (
        await session.execute(select(Project).order_by(Project.created_at.desc()).limit(100))
    ).scalars()
    result = []
    for project in projects:
        tasks = (
            await session.execute(
                select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())
            )
        ).scalars()
        result.append(project_json(project, list(tasks)))
    return result


@router.get("/projects/{project_id}")
async def get_project(project_id: str, session: SessionDep) -> dict[str, Any]:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    tasks = (
        await session.execute(
            select(Task).where(Task.project_id == project_id).order_by(Task.created_at.desc())
        )
    ).scalars()
    return project_json(project, list(tasks))


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, session: SessionDep) -> Response:
    running = (
        await session.execute(
            select(Task.id).where(
                Task.project_id == project_id, Task.state.in_([s.value for s in ACTIVE])
            )
        )
    ).first()
    if running:
        raise HTTPException(409, "a task in this project is still running; cancel it first")
    await session.execute(delete(Project).where(Project.id == project_id))
    await session.commit()
    return Response(status_code=204)


# --- tasks -------------------------------------------------------------------------------


class TaskCreate(BaseModel):
    description: str = Field(min_length=10, max_length=6000)


class Rejection(BaseModel):
    feedback: str = Field(default="", max_length=3000)


async def _starting_archive(session: AsyncSession, project_id: str) -> str | None:
    """A new task starts from the project's latest finished work, else from the upload."""
    done = (
        await session.execute(
            select(Task.result_artifact_id)
            .where(
                Task.project_id == project_id,
                Task.state == S.COMPLETED.value,
                Task.result_artifact_id.is_not(None),
            )
            .order_by(Task.finished_at.desc())
            .limit(1)
        )
    ).scalar()
    if done:
        return done
    return (
        await session.execute(
            select(Artifact.id)
            .where(Artifact.project_id == project_id, Artifact.kind == "source")
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
    ).scalar()


@router.post("/projects/{project_id}/tasks", status_code=201)
async def create_task(
    project_id: str, body: TaskCreate, runtime: RuntimeDep, session: SessionDep
) -> dict[str, Any]:
    if await session.get(Project, project_id) is None:
        raise HTTPException(404, "project not found")
    task = Task(
        project_id=project_id,
        description=body.description.strip(),
        state=S.QUEUED.value,
        base_artifact_id=await _starting_archive(session, project_id),
        reports={},
        usage={},
    )
    session.add(task)
    await session.commit()
    await runtime.bus.emit(task.id, "task_created", {"description": task.description})
    await enqueue(runtime.redis, task.id)
    return task_json(task)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, session: SessionDep) -> dict[str, Any]:
    return task_json(await _task(session, task_id))


async def _move(
    session: AsyncSession, task_id: str, current: TaskState, target: TaskState, **values: Any
) -> bool:
    """Compare-and-set a state change made by a user action."""
    if target in TERMINAL:
        values["finished_at"] = now()
    result = await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state == current.value)
        .values(state=target.value, **values)
    )
    await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


@router.post("/tasks/{task_id}/approve")
async def approve_plan(task_id: str, runtime: RuntimeDep, session: SessionDep) -> dict[str, Any]:
    await _task(session, task_id)
    if not await _move(session, task_id, S.AWAITING_APPROVAL, S.PROVISIONING):
        raise HTTPException(409, "this task is not waiting for approval")
    await runtime.bus.emit(task_id, "plan_approved", {})
    await runtime.bus.emit(
        task_id, "state_changed", {"from": S.AWAITING_APPROVAL.value, "to": S.PROVISIONING.value}
    )
    await enqueue(runtime.redis, task_id)
    session.expire_all()
    return task_json(await _task(session, task_id))


@router.post("/tasks/{task_id}/reject")
async def reject_plan(
    task_id: str, body: Rejection, runtime: RuntimeDep, session: SessionDep
) -> dict[str, Any]:
    """Reject the plan. With feedback the planner revises it; without, the task ends."""
    task = await _task(session, task_id)
    feedback = body.feedback.strip()
    if feedback and task.plan_revisions < runtime.settings.max_plan_revisions:
        moved = await _move(
            session, task_id, S.AWAITING_APPROVAL, S.PLANNING,
            plan_feedback=feedback, plan_revisions=task.plan_revisions + 1,
        )  # fmt: skip
        target = S.PLANNING
    else:
        moved = await _move(
            session, task_id, S.AWAITING_APPROVAL, S.REJECTED,
            error="plan rejected by the user",
        )  # fmt: skip
        target = S.REJECTED
    if not moved:
        raise HTTPException(409, "this task is not waiting for approval")
    await runtime.bus.emit(task_id, "plan_rejected", {"feedback": feedback})
    await runtime.bus.emit(
        task_id, "state_changed", {"from": S.AWAITING_APPROVAL.value, "to": target.value}
    )
    if target == S.PLANNING:
        await enqueue(runtime.redis, task_id)
    session.expire_all()
    return task_json(await _task(session, task_id))


@router.post("/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(task_id: str, runtime: RuntimeDep, session: SessionDep) -> dict[str, Any]:
    task = await _task(session, task_id)
    state = S(task.state)
    if state in TERMINAL:
        raise HTTPException(409, "this task has already finished")
    # Tell a worker that may be running it; it stops at the next step or kills the command.
    await runtime.redis.set(cancel_key(task_id), "1", ex=3600)
    if state in (S.QUEUED, S.AWAITING_APPROVAL) and await _move(
        session, task_id, state, S.CANCELLED, error="cancelled by the user"
    ):
        await runtime.bus.emit(
            task_id, "state_changed", {"from": state.value, "to": S.CANCELLED.value}
        )
    session.expire_all()
    return task_json(await _task(session, task_id))


# --- events ------------------------------------------------------------------------------


@router.get("/tasks/{task_id}/events")
async def list_events(
    task_id: str, session: SessionDep, after: int = 0, limit: Annotated[int, Query(le=2000)] = 1000
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Event)
            .where(Event.task_id == task_id, Event.id > after)
            .order_by(Event.id)
            .limit(limit)
        )
    ).scalars()
    return [serialise(e) for e in rows]


def _sse(event: dict[str, Any]) -> str:
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"


async def _stream(
    runtime: Runtime, request: Request, task_id: str, after: int
) -> AsyncIterator[str]:
    """Replay stored events after `after`, then follow new ones until the task ends.

    Redis only wakes this loop up; the events themselves are always read from Postgres, so
    a missed publish delays an event by one keep-alive interval instead of losing it."""
    pubsub = runtime.redis.pubsub()
    await pubsub.subscribe(channel(task_id))
    last = after
    try:
        while True:
            async with runtime.sessions() as session:
                rows = list(
                    (
                        await session.execute(
                            select(Event)
                            .where(Event.task_id == task_id, Event.id > last)
                            .order_by(Event.id)
                            .limit(500)
                        )
                    ).scalars()
                )
                state = (
                    await session.execute(select(Task.state).where(Task.id == task_id))
                ).scalar()
            for row in rows:
                last = row.id
                yield _sse(serialise(row))
            if len(rows) == 500:
                continue
            if state is None or S(state) in TERMINAL:
                if rows:
                    continue  # read once more: the last events may have landed after the state
                yield "event: end\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=10)
            if message is None:
                yield ": keep-alive\n\n"
            else:
                # Drain the burst so one database read covers many notifications.
                while await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.01):
                    pass
                await asyncio.sleep(0.05)
    finally:
        await pubsub.unsubscribe(channel(task_id))
        await pubsub.aclose()  # type: ignore[no-untyped-call]


@router.get("/tasks/{task_id}/stream")
async def stream_events(
    task_id: str, request: Request, runtime: RuntimeDep, session: SessionDep, after: int = 0
) -> StreamingResponse:
    await _task(session, task_id)
    last_id = request.headers.get("last-event-id")
    if last_id and last_id.isdigit():
        after = max(after, int(last_id))
    return StreamingResponse(
        _stream(runtime, request, task_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- files, diffs, executions --------------------------------------------------------------


async def _archive(session: AsyncSession, artifact_id: str | None) -> bytes | None:
    if artifact_id is None:
        return None
    artifact = await session.get(Artifact, artifact_id)
    return artifact.data if artifact else None


async def _workspace(runtime: Runtime, session: AsyncSession, task: Task) -> Workspace:
    """Where the task's newest files are: its live sandbox, else its result, else its base."""
    if S(task.state) in SANDBOX_STATES:
        sandbox = await runtime.driver.attach(task.id)
        if sandbox is not None:
            return sandbox
    return ArchiveWorkspace(
        await _archive(session, task.result_artifact_id or task.base_artifact_id)
    )


async def _file_op(workspace: Workspace, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """A read against a live sandbox can collide with the end-of-command process sweep;
    one retry covers it."""
    for attempt in (1, 2):
        try:
            return await workspace.file_op(op, args)
        except SandboxToolError as exc:
            raise HTTPException(404 if exc.code == "not_found" else 422, exc.message) from exc
        except SandboxError as exc:
            if attempt == 2:
                raise HTTPException(503, "sandbox is busy; try again") from exc
            await asyncio.sleep(0.3)
    raise AssertionError("unreachable")


@router.get("/tasks/{task_id}/files")
async def list_files(task_id: str, runtime: RuntimeDep, session: SessionDep) -> dict[str, Any]:
    task = await _task(session, task_id)
    workspace = await _workspace(runtime, session, task)
    listing = await _file_op(workspace, "list_files", {"path": ".", "max_entries": 2000})
    return {"files": [e for e in listing["entries"] if e["type"] == "file"]}


@router.get("/tasks/{task_id}/file")
async def read_file(
    task_id: str, path: str, runtime: RuntimeDep, session: SessionDep, version: str = "current"
) -> dict[str, Any]:
    task = await _task(session, task_id)
    if version == "base":
        workspace: Workspace = ArchiveWorkspace(await _archive(session, task.base_artifact_id))
        try:
            return await workspace.file_op("read_file", {"path": path})
        except SandboxToolError:
            return {"path": path, "content": "", "missing": True}
    workspace = await _workspace(runtime, session, task)
    try:
        return await _file_op(workspace, "read_file", {"path": path})
    except HTTPException as exc:
        if exc.status_code == 404:
            return {"path": path, "content": "", "missing": True}
        raise


@router.get("/tasks/{task_id}/diff")
async def get_diff(task_id: str, runtime: RuntimeDep, session: SessionDep) -> dict[str, Any]:
    """Changed files and the unified diff: live from the sandbox while the task runs."""
    task = await _task(session, task_id)
    if S(task.state) in SANDBOX_STATES:
        sandbox = await runtime.driver.attach(task.id)
        if sandbox is not None:
            try:
                base = await git.base_commit(sandbox)
                return {
                    "live": True,
                    "changes": await git.changes(sandbox, base, viewer=True),
                    "diff": await git.diff(sandbox, base, viewer=True),
                }
            except (SandboxError, IndexError):
                pass  # between commands, or the sandbox just ended: fall back to what is stored
    return {"live": False, "changes": task.changes or [], "diff": task.final_diff or ""}


@router.get("/tasks/{task_id}/executions")
async def list_executions(task_id: str, session: SessionDep) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Execution).where(Execution.task_id == task_id).order_by(Execution.id)
        )
    ).scalars()
    return [
        {
            "id": e.id,
            "agent": e.agent,
            "kind": e.kind,
            "command": e.command,
            "exit_code": e.exit_code,
            "timed_out": e.timed_out,
            "duration_ms": e.duration_ms,
            "output": e.output,
            "at": e.created_at.isoformat(),
        }
        for e in rows
    ]


@router.get("/tasks/{task_id}/download")
async def download(task_id: str, session: SessionDep) -> Response:
    task = await _task(session, task_id)
    if task.result_artifact_id is None:
        raise HTTPException(409, "this task has no result to download yet")
    artifact = await session.get(Artifact, task.result_artifact_id)
    if artifact is None:
        raise HTTPException(404, "result archive not found")
    return Response(
        artifact.data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
    )


@router.get("/tasks/{task_id}/export")
async def export_run(task_id: str, session: SessionDep) -> dict[str, Any]:
    """Everything needed to replay a finished run without a backend: the task, its events,
    and the text of its files before and after."""
    task = await _task(session, task_id)
    if S(task.state) not in TERMINAL:
        raise HTTPException(409, "only a finished task can be exported")
    project = await session.get(Project, task.project_id)
    events = (
        await session.execute(select(Event).where(Event.task_id == task_id).order_by(Event.id))
    ).scalars()
    executions = await list_executions(task_id, session)

    def texts(data: bytes | None) -> dict[str, str]:
        files = ArchiveWorkspace(data).files
        return {
            path: raw.decode("utf-8", errors="replace")
            for path, raw in files.items()
            if len(raw) <= 200_000 and b"\x00" not in raw[:8192]
        }

    current = texts(await _archive(session, task.result_artifact_id))
    base = texts(await _archive(session, task.base_artifact_id))
    changed = {c["path"] for c in (task.changes or [])}
    return {
        "project": project_json(project) if project else None,
        "task": task_json(task),
        "events": [serialise(e) for e in events],
        "executions": executions,
        "files": current,
        "base_files": {p: c for p, c in base.items() if p in changed},
        "diff": task.final_diff or "",
    }
