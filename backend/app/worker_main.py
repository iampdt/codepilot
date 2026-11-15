"""Run the worker as its own process: `python -m app.worker_main`."""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.db import dispose_engine
from app.logging import configure_logging
from app.runtime import build_runtime
from app.worker import Worker


async def main() -> None:
    settings = get_settings()
    configure_logging(json=settings.log_json or settings.env == "production")
    runtime = build_runtime(settings)
    worker = Worker(runtime.redis, runtime.orchestrator, settings.worker_concurrency)
    try:
        await worker.run_forever()
    finally:
        await worker.stop()
        await runtime.redis.aclose()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
