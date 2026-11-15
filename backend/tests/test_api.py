"""The HTTP API against the test database. No worker runs: these check what the API itself
decides (validation, state changes, what gets queued)."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events import EventBus
from app.main import create_app
from app.models import Artifact, Task
from app.runtime import build_runtime
from app.worker import QUEUE
from tests.test_archive import make_zip

DESCRIPTION = "Add a health endpoint that returns the service version."


@pytest.fixture
async def client(redis: Redis) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.runtime = build_runtime()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    await app.state.runtime.redis.aclose()


async def make_task(client: httpx.AsyncClient, files: dict[str, str] | None = None) -> dict:
    upload = {"archive": ("code.zip", make_zip(files), "application/zip")} if files else None
    project = (await client.post("/api/projects", data={"name": "demo"}, files=upload)).json()
    response = await client.post(
        f"/api/projects/{project['id']}/tasks", json={"description": DESCRIPTION}
    )
    assert response.status_code == 201
    return response.json()


async def set_state(
    sessions: async_sessionmaker[AsyncSession], task_id: str, **values: object
) -> None:
    async with sessions() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(**values))
        await session.commit()


async def test_health_reports_an_unconfigured_model_without_failing(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok" and body["sandbox"] == "docker"
    assert body["llm"]["configured"] is False


async def test_creating_a_task_queues_it_and_starts_from_the_upload(
    client: httpx.AsyncClient, redis: Redis, sessions: async_sessionmaker[AsyncSession]
) -> None:
    task = await make_task(client, {"app.py": "print('hi')\n"})
    assert task["state"] == "queued"
    assert [item.decode() for item in await redis.lrange(QUEUE, 0, -1)] == [task["id"]]
    async with sessions() as session:
        row = await session.get(Task, task["id"])
        artifact = await session.get(Artifact, row.base_artifact_id)
    assert artifact.kind == "source"
    files = (await client.get(f"/api/tasks/{task['id']}/files")).json()["files"]
    assert [f["path"] for f in files] == ["app.py"]
    content = (await client.get(f"/api/tasks/{task['id']}/file", params={"path": "app.py"})).json()
    assert content["content"] == "print('hi')\n"


@pytest.mark.parametrize(
    ("files", "status"),
    [({"../evil.py": "x"}, 422), ({"/abs.py": "x"}, 422), ({"bomb": "0" * 5_000_000}, 422)],
)
async def test_unsafe_uploads_are_refused(
    client: httpx.AsyncClient, files: dict[str, str], status: int
) -> None:
    upload = {"archive": ("code.zip", make_zip(files), "application/zip")}
    assert (
        await client.post("/api/projects", data={"name": "x"}, files=upload)
    ).status_code == status
    not_zip = {"archive": ("code.zip", b"not a zip", "application/zip")}
    assert (
        await client.post("/api/projects", data={"name": "x"}, files=not_zip)
    ).status_code == 422


async def test_validation_of_task_and_project_input(client: httpx.AsyncClient) -> None:
    project = (await client.post("/api/projects", data={"name": "demo"})).json()
    assert (
        await client.post(f"/api/projects/{project['id']}/tasks", json={"description": "short"})
    ).status_code == 422
    assert (
        await client.post("/api/projects/nope/tasks", json={"description": DESCRIPTION})
    ).status_code == 404
    assert (await client.post("/api/projects", data={"name": ""})).status_code == 422
    assert (await client.get("/api/tasks/nope")).status_code == 404


async def test_approval_only_works_at_the_gate_and_queues_the_task(
    client: httpx.AsyncClient, redis: Redis, sessions: async_sessionmaker[AsyncSession]
) -> None:
    task = await make_task(client)
    assert (
        await client.post(f"/api/tasks/{task['id']}/approve")
    ).status_code == 409  # still queued
    await set_state(sessions, task["id"], state="awaiting_approval", plan={"summary": "p"})
    await redis.delete(QUEUE)
    approved = await client.post(f"/api/tasks/{task['id']}/approve")
    assert approved.status_code == 200 and approved.json()["state"] == "provisioning"
    assert await redis.llen(QUEUE) == 1
    assert (await client.post(f"/api/tasks/{task['id']}/approve")).status_code == 409  # not twice


async def test_reject_with_feedback_replans_and_without_ends_the_task(
    client: httpx.AsyncClient, redis: Redis, sessions: async_sessionmaker[AsyncSession]
) -> None:
    task = await make_task(client)
    await set_state(sessions, task["id"], state="awaiting_approval", plan={"summary": "p"})
    await redis.delete(QUEUE)
    revised = (
        await client.post(f"/api/tasks/{task['id']}/reject", json={"feedback": "Use Flask"})
    ).json()
    assert revised["state"] == "planning" and revised["plan_revisions"] == 1
    assert await redis.llen(QUEUE) == 1
    await set_state(sessions, task["id"], state="awaiting_approval")
    ended = (await client.post(f"/api/tasks/{task['id']}/reject", json={})).json()
    assert ended["state"] == "rejected" and ended["finished_at"]
    assert (await client.post(f"/api/tasks/{task['id']}/cancel")).status_code == 409


async def test_cancel_ends_a_waiting_task_and_flags_a_running_one(
    client: httpx.AsyncClient, redis: Redis, sessions: async_sessionmaker[AsyncSession]
) -> None:
    waiting = await make_task(client)
    assert (await client.post(f"/api/tasks/{waiting['id']}/cancel")).json()["state"] == "cancelled"
    running = await make_task(client)
    await set_state(sessions, running["id"], state="coding")
    response = await client.post(f"/api/tasks/{running['id']}/cancel")
    assert (
        response.status_code == 202 and response.json()["state"] == "coding"
    )  # the worker ends it
    assert await redis.exists(f"codepilot:cancel:{running['id']}")


async def test_stream_replays_stored_events_then_ends_for_a_finished_task(
    client: httpx.AsyncClient, redis: Redis, sessions: async_sessionmaker[AsyncSession]
) -> None:
    task = await make_task(client)
    bus = EventBus(sessions, redis)
    await bus.emit(task["id"], "agent_note", {"text": "one"})
    await bus.emit(task["id"], "agent_note", {"text": "two"})
    await set_state(sessions, task["id"], state="completed")
    listed = (await client.get(f"/api/tasks/{task['id']}/events")).json()
    assert [e["type"] for e in listed] == ["task_created", "agent_note", "agent_note"]
    body = (
        await client.get(f"/api/tasks/{task['id']}/stream", params={"after": listed[1]["id"]})
    ).text
    frames = [f for f in body.split("\n\n") if f.strip()]
    assert len(frames) == 2 and frames[-1].startswith("event: end")
    assert json.loads(frames[0].split("data: ", 1)[1])["payload"] == {"text": "two"}
    assert f"id: {listed[2]['id']}" in frames[0]


async def test_download_and_export_of_a_finished_task(
    client: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    task = await make_task(client, {"app.py": "old\n", "keep.py": "same\n"})
    assert (await client.get(f"/api/tasks/{task['id']}/download")).status_code == 409
    assert (await client.get(f"/api/tasks/{task['id']}/export")).status_code == 409
    result = make_zip({"app.py": "new\n", "keep.py": "same\n", "added.py": "x\n"})
    async with sessions() as session:
        artifact = Artifact(project_id=task["project_id"], task_id=task["id"], kind="result",
                            filename="out.zip", size=len(result), data=result)  # fmt: skip
        session.add(artifact)
        await session.commit()
    await set_state(sessions, task["id"], state="completed", result_artifact_id=artifact.id,
                    changes=[{"path": "app.py", "status": "modified", "added": 1, "removed": 1}],
                    final_diff="-old\n+new\n")  # fmt: skip
    download = await client.get(f"/api/tasks/{task['id']}/download")
    assert download.headers["content-type"] == "application/zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(download.content)).namelist()) == [
        "added.py",
        "app.py",
        "keep.py",
    ]
    bundle = (await client.get(f"/api/tasks/{task['id']}/export")).json()
    assert bundle["files"]["app.py"] == "new\n" and bundle["base_files"] == {"app.py": "old\n"}
    base = (
        await client.get(
            f"/api/tasks/{task['id']}/file", params={"path": "app.py", "version": "base"}
        )
    ).json()
    assert base["content"] == "old\n"
    missing = (
        await client.get(
            f"/api/tasks/{task['id']}/file", params={"path": "added.py", "version": "base"}
        )
    ).json()
    assert missing["missing"] is True
    # The next task in the project starts from this result, not from the original upload.
    following = (
        await client.post(
            f"/api/projects/{task['project_id']}/tasks", json={"description": DESCRIPTION}
        )
    ).json()
    files = (await client.get(f"/api/tasks/{following['id']}/files")).json()["files"]
    assert "added.py" in [f["path"] for f in files]


async def test_access_token_is_enforced_when_set(redis: Redis) -> None:
    app = create_app()
    runtime = build_runtime()
    runtime.settings = runtime.settings.model_copy(update={"api_token": "s3cret"})
    app.state.runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        assert (await http.get("/api/health")).status_code == 200
        assert (await http.get("/api/projects")).status_code == 401
        assert (
            await http.get("/api/projects", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await http.get("/api/projects", headers={"Authorization": "Bearer s3cret"})
        ).status_code == 200
        assert (await http.get("/api/projects", params={"token": "s3cret"})).status_code == 200
    await runtime.redis.aclose()
