import { describe, expect, it } from "vitest";
import { applyEvent, emptyView, reduceEvents } from "./reduce";
import type { RunEvent } from "./types";

let nextId = 0;
const ev = (type: string, payload: Record<string, any> = {}): RunEvent => ({
  id: ++nextId,
  task_id: "t",
  type,
  payload,
  at: "2026-11-15T10:00:00Z",
});

describe("reduceEvents", () => {
  it("follows state changes and records the path taken", () => {
    const view = reduceEvents([
      ev("state_changed", { from: "queued", to: "planning" }),
      ev("state_changed", { from: "planning", to: "awaiting_approval" }),
      ev("state_changed", { from: "verifying", to: "failed", error: "tests still failing" }),
    ]);
    expect(view.state).toBe("failed");
    expect(view.visited).toEqual(["queued", "planning", "awaiting_approval", "failed"]);
    expect(view.error).toBe("tests still failing");
  });

  it("assembles a command from its start, output chunks and end", () => {
    const view = reduceEvents([
      ev("command_started", { run_id: "r1", agent: "executor", kind: "test", command: "pytest -q", network: false }),
      ev("command_output", { run_id: "r1", text: "..F" }),
      ev("command_output", { run_id: "r1", text: "\n1 failed" }),
      ev("command_finished", { run_id: "r1", exit_code: 1, timed_out: false, duration_ms: 420 }),
    ]);
    expect(view.runs).toHaveLength(1);
    expect(view.runs[0]).toMatchObject({ output: "..F\n1 failed", exitCode: 1, durationMs: 420 });
  });

  it("ignores an event it has already applied", () => {
    const event = ev("file_changed", { path: "a.py", action: "created" });
    const view = emptyView();
    applyEvent(view, event);
    applyEvent(view, { ...event, payload: { path: "a.py", action: "deleted" } });
    expect(view.touched).toEqual({ "a.py": "created" });
  });

  it("shows a refused tool call with its reason", () => {
    const view = reduceEvents([
      ev("tool_call", { agent: "executor", tool: "execute_command", args: { command: "curl x" }, ok: false, error: { code: "command_not_allowed", message: "'curl' is not an allowed program" } }),
    ]);
    expect(view.timeline[0]).toMatchObject({ kind: "tool", ok: false });
    expect(view.timeline[0]?.detail).toContain("refused: command_not_allowed");
  });

  it("keeps the newest budget, plan, review and sandbox state", () => {
    const view = reduceEvents([
      ev("plan_ready", { plan: { summary: "p" } }),
      ev("sandbox_ready", { driver: "docker", layers: ["non-root"], limits: {} }),
      ev("llm_call", { agent: "coder", budget: { llm_calls: 3, total_tokens: 900 } }),
      ev("test_result", { agent: "orchestrator", kind: "verify", command: "pytest -q", passed: true, summary: { passed: 4 } }),
      ev("review_ready", { review: { verdict: "approve" } }),
      ev("sandbox_destroyed"),
    ]);
    expect(view.plan).toEqual({ summary: "p" });
    expect(view.usage.llm_calls).toBe(3);
    expect(view.tests[0]?.passed).toBe(true);
    expect(view.review?.verdict).toBe("approve");
    expect(view.sandboxAlive).toBe(false);
    expect(view.timeline.map((t) => t.title)).toContain("Verification passed");
  });
});
