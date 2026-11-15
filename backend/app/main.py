"""Application entry point."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import public, router
from app.config import get_settings
from app.db import dispose_engine
from app.logging import configure_logging, get_logger
from app.runtime import build_runtime
from app.worker import Worker

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(json=settings.log_json or settings.env == "production")
    runtime = build_runtime(settings)
    app.state.runtime = runtime
    worker: Worker | None = None
    worker_task: asyncio.Task[None] | None = None
    if settings.run_worker_in_api:
        worker = Worker(runtime.redis, runtime.orchestrator, settings.worker_concurrency)
        worker_task = asyncio.create_task(worker.run_forever())
    log.info("api_started", env=settings.env, worker_in_api=settings.run_worker_in_api)
    try:
        yield
    finally:
        if worker is not None and worker_task is not None:
            await worker.stop()
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        await runtime.redis.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="CodePilot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(public)
    app.include_router(router)
    return app


app = create_app()
