"""Budget, state table, schema conversion, history compaction and the model router."""

import pytest

from app.agents.base import compact
from app.agents.contracts import Plan
from app.llm.base import (
    LLMBadOutput,
    LLMError,
    LLMResponse,
    LLMUnavailable,
    Message,
    ToolCall,
    ToolOutcome,
)
from app.llm.gemini import _contents, to_gemini_schema
from app.llm.router import LLMExhausted, ModelRouter
from app.orchestrator.budget import Budget, BudgetExceeded, BudgetLimits
from app.orchestrator.state import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    TaskState,
    assert_transition,
)
from app.tools.registry import (
    ROLE_PERMISSIONS,
    InstallDependencyInput,
    Permission,
    digest_failures,
    format_failure,
    parse_test_summary,
    tools_for,
)

S = TaskState


def budget(**limits: int) -> Budget:
    base = {
        "max_llm_calls": 3,
        "max_total_tokens": 1000,
        "max_tool_calls": 2,
        "max_wall_seconds": 60,
    }
    return Budget(BudgetLimits(**{**base, **limits}))


def test_budget_stops_at_each_ceiling() -> None:
    b = budget()
    for _ in range(3):
        b.before_llm_call()
        b.charge_llm("coder", 10, 5)
    with pytest.raises(BudgetExceeded, match="model calls"):
        b.before_llm_call()
    b.charge_tool("coder")
    b.charge_tool("coder")
    with pytest.raises(BudgetExceeded, match="tool calls"):
        b.charge_tool("coder")
    assert b.by_agent["coder"] == {
        "llm_calls": 3,
        "input_tokens": 30,
        "output_tokens": 15,
        "tool_calls": 2,
    }


def test_budget_token_and_time_ceilings_and_restore() -> None:
    b = budget()
    b.charge_llm("planner", 900, 200)
    with pytest.raises(BudgetExceeded, match="tokens"):
        b.check()
    restored = Budget.restore(b.limits, {**b.snapshot(), "elapsed_seconds": 61})
    assert restored.llm_calls == 1 and restored.total_tokens == 1100
    with pytest.raises(BudgetExceeded, match="wall-clock"):
        budget().__class__.restore(b.limits, {"elapsed_seconds": 61}).check()


def test_every_state_has_a_row_and_terminal_states_have_no_exits() -> None:
    assert set(TRANSITIONS) == set(S)
    for state in TERMINAL:
        assert TRANSITIONS[state] == frozenset()
    assert_transition(S.AWAITING_APPROVAL, S.PROVISIONING)
    assert_transition(S.VERIFYING, S.DEBUGGING)
    for current, target in [(S.QUEUED, S.CODING), (S.PLANNING, S.CODING), (S.CODING, S.COMPLETED),
                            (S.COMPLETED, S.CODING), (S.DEBUGGING, S.COMPLETED)]:  # fmt: skip
        with pytest.raises(InvalidTransition):
            assert_transition(current, target)


def test_coding_is_unreachable_without_passing_the_approval_gate() -> None:
    """Every path from QUEUED to a state with a sandbox goes through AWAITING_APPROVAL."""
    seen, frontier = {S.QUEUED}, [S.QUEUED]
    while frontier:
        for target in TRANSITIONS[frontier.pop()]:
            if target not in seen and target != S.AWAITING_APPROVAL:
                seen.add(target)
                frontier.append(target)
    assert seen == {S.QUEUED, S.PLANNING, S.FAILED, S.CANCELLED}


def test_role_permissions_separate_writing_from_running() -> None:
    names = {role: {t.name for t in tools_for(role)} for role in ROLE_PERMISSIONS}
    assert names["planner"] == {"list_files", "read_file", "search_code"}
    assert "execute_command" not in names["coder"] and "write_file" in names["coder"]
    assert "write_file" not in names["executor"] and "run_tests" in names["executor"]
    assert names["reviewer"].isdisjoint({"write_file", "edit_file", "delete_file", "execute_command",
                                         "install_dependency", "restore_checkpoint"})  # fmt: skip
    assert Permission.INSTALL not in ROLE_PERMISSIONS["coder"]
    assert len(names["debugger"]) == 13


def test_gemini_schema_drops_unsupported_keywords_and_inlines_refs() -> None:
    schema = to_gemini_schema(Plan.model_json_schema())
    step = schema["properties"]["steps"]["items"]
    assert step["type"] == "object" and set(step["properties"]) == {
        "id",
        "title",
        "description",
        "files",
    }
    assert "$defs" not in schema and "title" not in schema and "minLength" not in str(schema)
    nullable = to_gemini_schema(InstallDependencyInput.model_json_schema())["properties"][
        "requirements_file"
    ]
    assert nullable["type"] == "string" and nullable["nullable"] is True


def test_gemini_history_returns_the_model_turn_unchanged() -> None:
    raw = [
        {"functionCall": {"name": "read_file", "args": {"path": "a"}}, "thoughtSignature": "sig"}
    ]
    contents = _contents(
        [
            Message(role="user", text="go"),
            Message(role="assistant", tool_calls=[ToolCall("1", "read_file", {"path": "a"})], raw=raw,
                    provider="gemini"),
            Message(role="tool", tool_results=[ToolOutcome("1", "read_file", {"ok": True})]),
        ]
    )  # fmt: skip
    assert contents[1] == {"role": "model", "parts": raw}
    assert contents[2]["parts"][0]["functionResponse"] == {
        "name": "read_file",
        "response": {"ok": True},
    }


def test_compaction_stubs_old_bulk_and_keeps_the_brief_and_recent_turns() -> None:
    big = "x" * 5000
    history = [Message(role="user", text="brief " + big)]
    for i in range(8):
        history.append(Message(role="assistant", provider="gemini",
                               tool_calls=[ToolCall(str(i), "write_file", {"path": "a", "content": big})],
                               raw=[{"functionCall": {"name": "write_file", "args": {"content": big}},
                                     "thoughtSignature": "keep"}]))  # fmt: skip
        history.append(
            Message(role="tool", tool_results=[ToolOutcome(str(i), "write_file", {"content": big})])
        )
    slim = compact(history)
    assert len(slim) == len(history) and slim[0].text == history[0].text
    assert "elided" in slim[1].tool_calls[0].args["content"]
    assert "elided" in slim[1].raw[0]["functionCall"]["args"]["content"]
    assert slim[1].raw[0]["thoughtSignature"] == "keep"
    assert "elided" in slim[2].tool_results[0].content["content"]
    assert slim[-1].tool_results[0].content["content"] == big
    assert history[1].raw[0]["functionCall"]["args"]["content"] == big  # the original is untouched


def test_pytest_summary_is_parsed() -> None:
    assert parse_test_summary("...\n==== 3 passed, 1 failed in 0.2s ====\n") == {
        "passed": 3,
        "failed": 1,
    }
    assert parse_test_summary("no summary here") == {}


class _Flaky:
    name = "flaky"

    def __init__(self, outcomes: dict[str, list[object]]) -> None:
        self.outcomes = outcomes
        self.seen: list[str] = []

    async def complete(self, *, model: str, **_: object) -> LLMResponse:
        self.seen.append(model)
        outcome = self.outcomes[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResponse(message=Message(role="assistant"), model=model)


async def test_router_falls_through_the_chain_then_waits_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("app.llm.router.asyncio.sleep", fake_sleep)
    provider = _Flaky(
        {
            "a": [LLMUnavailable("429", retry_after=12.0), LLMBadOutput("junk")],
            "b": [LLMUnavailable("503"), "ok"],
        }
    )
    router = ModelRouter(provider, ["a", "b"])
    notices: list[float] = []

    async def notice(_error: str, wait: float) -> None:
        notices.append(wait)

    response = await router.complete(system="s", messages=[], tools=[], on_retry=notice)
    assert response.model == "b" and provider.seen == ["a", "b", "a", "b"]
    assert waits == [12.0] and notices == [12.0]


async def test_router_gives_up_after_bounded_rounds_and_does_not_retry_fatal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.llm.router.asyncio.sleep", fake_sleep)
    down = _Flaky({"a": [LLMUnavailable("503")] * 10})
    with pytest.raises(LLMExhausted):
        await ModelRouter(down, ["a"]).complete(system="s", messages=[], tools=[])
    assert len(down.seen) == 4
    fatal = _Flaky({"a": [LLMError("400 bad key")]})
    with pytest.raises(LLMError, match="bad key"):
        await ModelRouter(fatal, ["a"]).complete(system="s", messages=[], tools=[])
    assert fatal.seen == ["a"]


def test_failure_digest_keeps_the_cause_and_drops_the_bulk() -> None:
    block = (
        "_________________________ test_register _________________________\n"
        "    def test_register():\n>       client.post('/register')\n"
        "E   sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: users\n"
    )
    noise = (
        "  /site-packages/passlib/utils.py:854: DeprecationWarning: 'crypt' is deprecated\n" * 3000
    )
    output = (
        "=== FAILURES ===\n"
        + block
        + block.replace("test_register", "test_login") * 7
        + noise
        + "FAILED tests/test_auth.py::test_register - sqlalchemy.exc.OperationalErr...\n"
        + "FAILED tests/test_auth.py::test_login - sqlalchemy.exc.OperationalErr...\n"
        + "=== 8 failed, 1 passed in 2.6s ===\n"
    )
    assert len(output) > 200_000 and "no such table" not in output[-9000:]  # the tail hides it
    digest = digest_failures(output)
    assert digest["errors"] == [
        "sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: users"
    ]
    assert digest["failed_tests"] == [
        "tests/test_auth.py::test_register",
        "tests/test_auth.py::test_login",
    ]
    assert (
        digest["first_failure"].startswith("____") and "test_login" not in digest["first_failure"]
    )
    text = format_failure(output)
    assert "no such table: users" in text and len(text) < 4000


def test_failure_digest_falls_back_to_the_tail_for_other_runners() -> None:
    assert (
        format_failure("not ok 1 - adds numbers\n# fail 1") == "not ok 1 - adds numbers\n# fail 1"
    )
