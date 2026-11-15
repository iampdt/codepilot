"""Wires the long-lived objects together: database, Redis, event bus, model router, sandbox
driver, orchestrator. Built once at startup and shared by the API and the worker."""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import session_factory
from app.events import EventBus
from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, ToolSpec
from app.llm.router import ModelRouter, build_router
from app.logging import get_logger
from app.orchestrator.machine import Orchestrator
from app.sandbox.docker_driver import DockerDriver

log = get_logger("runtime")


class _Unconfigured:
    """Stands in for a provider when no key is set, so the API can start and say so."""

    name = "unconfigured"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        timeout_seconds: float,
    ) -> LLMResponse:
        raise LLMError(f"no model provider is configured: {self._reason}")


@dataclass
class Runtime:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis
    bus: EventBus
    router: ModelRouter
    driver: DockerDriver
    orchestrator: Orchestrator

    @property
    def llm_configured(self) -> bool:
        return self.router.provider.name != "unconfigured"


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    sessions = session_factory()
    redis = Redis.from_url(settings.redis_url)
    bus = EventBus(sessions, redis)
    try:
        router = build_router(settings)
    except LLMError as exc:
        log.warning("llm_unconfigured", reason=str(exc))
        placeholder: LLMProvider = _Unconfigured(str(exc))
        router = ModelRouter(placeholder, settings.model_chain or ["none"])
    driver = DockerDriver(
        image=settings.sandbox_image,
        network=settings.sandbox_network,
        proxy_url=settings.egress_proxy_url,
        runtime=settings.sandbox_runtime,
    )
    orchestrator = Orchestrator(
        settings=settings, sessions=sessions, bus=bus, router=router, driver=driver, redis=redis
    )
    return Runtime(settings, sessions, redis, bus, router, driver, orchestrator)
