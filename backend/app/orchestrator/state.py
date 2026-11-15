"""Task states and the only transitions allowed between them."""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    PROVISIONING = "provisioning"
    CODING = "coding"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    DEBUGGING = "debugging"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


S = TaskState

TERMINAL = frozenset({S.COMPLETED, S.FAILED, S.REJECTED, S.CANCELLED})
# States in which a worker is actively driving the task.
ACTIVE = frozenset(
    {S.PLANNING, S.PROVISIONING, S.CODING, S.EXECUTING, S.VERIFYING, S.DEBUGGING, S.REVIEWING}
)

TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    S.QUEUED: frozenset({S.PLANNING, S.CANCELLED, S.FAILED}),
    S.PLANNING: frozenset({S.AWAITING_APPROVAL, S.FAILED, S.CANCELLED}),
    # Approve → provisioning; reject with feedback → planning again; reject outright → rejected.
    S.AWAITING_APPROVAL: frozenset({S.PROVISIONING, S.PLANNING, S.REJECTED, S.CANCELLED}),
    S.PROVISIONING: frozenset({S.CODING, S.FAILED, S.CANCELLED}),
    S.CODING: frozenset({S.EXECUTING, S.FAILED, S.CANCELLED}),
    S.EXECUTING: frozenset({S.VERIFYING, S.FAILED, S.CANCELLED}),
    S.VERIFYING: frozenset({S.REVIEWING, S.DEBUGGING, S.FAILED, S.CANCELLED}),
    S.DEBUGGING: frozenset({S.VERIFYING, S.FAILED, S.CANCELLED}),
    # Review may send the work back to the coder once, with its findings.
    S.REVIEWING: frozenset({S.COMPLETED, S.CODING, S.FAILED, S.CANCELLED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: TaskState, target: TaskState) -> None:
        super().__init__(f"transition {current.value} → {target.value} is not allowed")
        self.current = current
        self.target = target


def assert_transition(current: TaskState, target: TaskState) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(current, target)
