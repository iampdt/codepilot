"""Task worker: takes task ids off the Redis queue and runs the orchestrator on each.

A task is run by one worker at a time. The worker holds a Redis lock for the task and
refreshes it while it works; if the worker dies the lock expires, and the next worker to
start sees an active task with no lock and recovers it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from redis.asyncio import Redis

from app.logging import get_logger
from app.orchestrator.machine import Orchestrator, cancel_key

log = get_logger("worker")

QUEUE = "codepilot:jobs"
LOCK_SECONDS = 30


def lock_key(task_id: str) -> str:
    return f"codepilot:lock:{task_id}"


async def enqueue(redis: Redis, task_id: str) -> None:
    await redis.rpush(QUEUE, task_id)


class Worker:
    def __init__(self, redis: Redis, orchestrator: Orchestrator, concurrency: int = 2) -> None:
        self._redis = redis
        self._orchestrator = orchestrator
        self._slots = asyncio.Semaphore(concurrency)
        self._id = uuid.uuid4().hex[:8]
        self._running: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    async def _is_locked(self, task_id: str) -> bool:
        return bool(await self._redis.exists(lock_key(task_id)))

    async def run_forever(self) -> None:
        failed, requeue = await self._orchestrator.recover(self._is_locked)
        queued = {
            item.decode() if isinstance(item, bytes) else item
            for item in await self._redis.lrange(QUEUE, 0, -1)
        }
        for task_id in requeue:
            if task_id not in queued:
                await enqueue(self._redis, task_id)
        log.info("worker_started", worker=self._id, failed=failed, requeued=len(requeue))
        while not self._stopping.is_set():
            await self._slots.acquire()
            try:
                item = await self._redis.blpop([QUEUE], timeout=2)
            except Exception as exc:  # noqa: BLE001 - Redis hiccup: back off and keep serving
                self._slots.release()
                log.warning("queue_error", error=str(exc))
                await asyncio.sleep(2)
                continue
            if item is None:
                self._slots.release()
                continue
            raw = item[1]
            task_id = raw.decode() if isinstance(raw, bytes) else str(raw)
            job = asyncio.create_task(self._run_one(task_id))
            self._running.add(job)
            job.add_done_callback(self._running.discard)

    async def _run_one(self, task_id: str) -> None:
        try:
            acquired = await self._redis.set(lock_key(task_id), self._id, nx=True, ex=LOCK_SECONDS)
            if not acquired:
                log.info("task_already_running", task_id=task_id)
                return
            heartbeat = asyncio.create_task(self._heartbeat(task_id))
            try:
                await self._orchestrator.run(task_id)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                await self._redis.delete(lock_key(task_id), cancel_key(task_id))
        except Exception:  # noqa: BLE001
            log.exception("worker_job_failed", task_id=task_id)
        finally:
            self._slots.release()

    async def _heartbeat(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(LOCK_SECONDS / 3)
            with contextlib.suppress(Exception):
                await self._redis.expire(lock_key(task_id), LOCK_SECONDS)

    async def stop(self) -> None:
        self._stopping.set()
        for job in list(self._running):
            job.cancel()
        for job in list(self._running):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job
