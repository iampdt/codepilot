"""Task contracts: the only things agents hand to the orchestrator.

Agents never talk to each other. Each one is given a typed input by the orchestrator and must
finish by submitting one of these models; the orchestrator validates it, stores it, and decides
the next state. Free text an agent produces along the way goes nowhere.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class PlanStep(Contract):
    id: int = Field(ge=1)
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=3, max_length=1500)
    files: list[str] = Field(default_factory=list, max_length=30)


class Plan(Contract):
    summary: str = Field(min_length=10, max_length=1200)
    approach: str = Field(min_length=10, max_length=2500)
    steps: list[PlanStep] = Field(min_length=1, max_length=15)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=15)
    risks: list[str] = Field(default_factory=list, max_length=10)
    dependencies: list[str] = Field(default_factory=list, max_length=40)
    test_strategy: str = Field(min_length=5, max_length=1500)
    test_command: str = Field(min_length=3, max_length=300)


class CodingReport(Contract):
    summary: str = Field(min_length=5, max_length=2500)
    files_changed: list[str] = Field(default_factory=list, max_length=200)
    test_command: str = Field(min_length=3, max_length=300)
    notes: str = Field(default="", max_length=2000)


class ExecutionReport(Contract):
    summary: str = Field(min_length=5, max_length=2500)
    dependencies_installed: bool
    test_command: str = Field(min_length=3, max_length=300)
    tests_passed: bool
    failures: list[str] = Field(default_factory=list, max_length=30)


class DebugReport(Contract):
    root_cause: str = Field(min_length=5, max_length=2500)
    fix_summary: str = Field(min_length=5, max_length=2500)
    files_changed: list[str] = Field(default_factory=list, max_length=100)
    tests_passing: bool


class CriterionCheck(Contract):
    criterion: str = Field(max_length=600)
    met: bool
    evidence: str = Field(default="", max_length=1000)


class ReviewFinding(Contract):
    severity: Literal["blocking", "major", "minor", "info"]
    file: str = Field(default="", max_length=300)
    issue: str = Field(min_length=3, max_length=1500)
    suggestion: str = Field(default="", max_length=1500)


class ReviewReport(Contract):
    verdict: Literal["approve", "request_changes"]
    summary: str = Field(min_length=5, max_length=3000)
    criteria: list[CriterionCheck] = Field(default_factory=list, max_length=20)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=30)
    security_notes: str = Field(default="", max_length=2000)

    @property
    def blocking(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity == "blocking"]
