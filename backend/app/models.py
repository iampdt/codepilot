"""Tables. Task state lives here, not in worker memory, so a run can stop at the approval
gate and be picked up again by any worker."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Json = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    plan: Mapped[dict[str, Any] | None] = mapped_column(Json, nullable=True)
    plan_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_revisions: Mapped[int] = mapped_column(Integer, default=0)
    # Typed reports from each agent, keyed by role; debug reports are a list.
    reports: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)

    debug_iterations: Mapped[int] = mapped_column(Integer, default=0)
    review_rounds: Mapped[int] = mapped_column(Integer, default=0)
    tests_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    test_command: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The archive this task started from, and the one it produced.
    base_artifact_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result_artifact_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    final_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes: Mapped[list[dict[str, Any]] | None] = mapped_column(Json, nullable=True)
    isolation: Mapped[dict[str, Any] | None] = mapped_column(Json, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    """Append-only record of everything a run did. The live stream and the replay read this."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (Index("ix_events_task_id_id", "task_id", "id"),)


class Execution(Base):
    """One command run in a sandbox, with its full (capped) output."""

    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(16))  # command | install | test | verify
    command: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int] = mapped_column(Integer)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer)
    output: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Artifact(Base):
    """A zip archive: an uploaded codebase, or the workspace a finished task produced.

    Stored in Postgres so results survive on hosts with no persistent disk."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # source | result
    filename: Mapped[str] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
