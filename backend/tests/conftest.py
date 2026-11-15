from __future__ import annotations

import os

# Point the app at the test database and a separate Redis database before it is imported.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://codepilot:codepilot@localhost:5434/codepilot_test"
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/1")
os.environ["RUN_WORKER_IN_API"] = "false"
os.environ["GEMINI_API_KEY"] = ""
os.environ["API_TOKEN"] = ""

from collections.abc import AsyncIterator, Callable  # noqa: E402

import pytest  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.db import get_engine, session_factory  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.llm.router import ModelRouter  # noqa: E402
from app.models import Base, Project, Task  # noqa: E402
from app.orchestrator.machine import Orchestrator  # noqa: E402
from tests.support import LocalDriver, Script, ScriptedProvider  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def schema() -> AsyncIterator[None]:
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture
def sessions() -> async_sessionmaker[AsyncSession]:
    return session_factory()


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(get_settings().redis_url)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={"max_debug_iterations": 2, "coder_max_steps": 8, "debugger_max_steps": 6}
    )


@pytest.fixture
def make_orchestrator(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
) -> Callable[..., tuple[Orchestrator, ScriptedProvider, LocalDriver]]:
    def build(
        script: Script, **overrides: object
    ) -> tuple[Orchestrator, ScriptedProvider, LocalDriver]:
        provider = ScriptedProvider(script)
        driver = LocalDriver()
        orchestrator = Orchestrator(
            settings=settings.model_copy(update=overrides),
            sessions=sessions,
            bus=EventBus(sessions, redis),
            router=ModelRouter(provider, ["scripted-model"]),
            driver=driver,
            redis=redis,
        )
        return orchestrator, provider, driver

    return build


@pytest.fixture
async def new_task(sessions: async_sessionmaker[AsyncSession]) -> Callable[..., object]:
    async def create(description: str = "Add a calculator with an add function") -> str:
        async with sessions() as session:
            project = Project(name="test project")
            session.add(project)
            await session.flush()
            task = Task(project_id=project.id, description=description, reports={}, usage={})
            session.add(task)
            await session.commit()
            return task.id

    return create
