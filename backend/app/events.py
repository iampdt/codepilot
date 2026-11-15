"""Event bus: every event is stored, then published for anyone watching live.

Postgres is the record (a reconnecting client replays from its last id); Redis pub/sub is
only the nudge that says a new row exists. Losing a Redis message therefore loses nothing.
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logging import get_logger
from app.models import Event

log = get_logger("events")


def channel(task_id: str) -> str:
    return f"codepilot:events:{task_id}"


def serialise(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "payload": event.payload,
        "at": event.created_at.isoformat(),
    }


class EventBus:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], redis: Redis | None) -> None:
        self._sessions = sessions
        self._redis = redis

    async def emit(self, task_id: str, type_: str, payload: dict[str, Any] | None = None) -> None:
        async with self._sessions() as session:
            event = Event(task_id=task_id, type=type_, payload=payload or {})
            session.add(event)
            await session.commit()
            message = serialise(event)
        if self._redis is not None:
            try:
                await self._redis.publish(channel(task_id), json.dumps(message))
            except Exception as exc:  # noqa: BLE001 - the stored row is what matters
                log.warning("event_publish_failed", task_id=task_id, error=str(exc))
