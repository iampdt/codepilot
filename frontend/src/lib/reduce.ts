// Turns the event log of a run into what the screen shows. Live and replayed runs go through
// the same function, so the demo can't drift from the real product: it is the same code fed
// the same events, only from a file instead of a socket.

import type { Agent, Change, Isolation, Plan, Review, RunEvent, TaskState, Usage } from "./types";

export interface TimelineItem {
  id: number;
  at: string;
  kind: "state" | "agent" | "tool" | "note" | "retry" | "test" | "checkpoint" | "info" | "error";
  agent?: Agent;
  title: string;
  detail?: string;
  ok?: boolean;
}

export interface TerminalRun {
  runId: string;
  agent: string;
  kind: string;
  command: string;
  network: boolean;
  output: string;
  exitCode: number | null;
  timedOut: boolean;
  durationMs: number | null;
  startedAt: string;
}

export interface TestResult {
  at: string;
  agent: string;
  kind: string;
  command: string;
  passed: boolean;
  summary: Record<string, number>;
}

export interface RunView {
  state: TaskState;
  visited: TaskState[];
  error: string | null;
  plan: Plan | null;
  review: Review | null;
  timeline: TimelineItem[];
  runs: TerminalRun[];
  tests: TestResult[];
  touched: Record<string, "created" | "modified" | "deleted">;
  changes: Change[] | null;
  usage: Partial<Usage>;
  isolation: Isolation | null;
  stats: { cpu_percent?: string; memory?: string; pids?: string } | null;
  activeAgent: Agent | null;
  sandboxAlive: boolean;
  lastEventId: number;
}

export function emptyView(): RunView {
  return {
    state: "queued",
    visited: ["queued"],
    error: null,
    plan: null,
    review: null,
    timeline: [],
    runs: [],
    tests: [],
    touched: {},
    changes: null,
    usage: {},
    isolation: null,
    stats: null,
    activeAgent: null,
    sandboxAlive: false,
    lastEventId: 0,
  };
}

const STATE_TITLES: Record<TaskState, string> = {
  queued: "Queued",
  planning: "Planning",
  awaiting_approval: "Plan ready — waiting for approval",
  provisioning: "Starting the sandbox",
  coding: "Writing code",
  executing: "Installing and running",
  verifying: "Verifying: the orchestrator runs the tests itself",
  debugging: "Debugging the failure",
  reviewing: "Reviewing the change",
  completed: "Completed",
  failed: "Failed",
  rejected: "Plan rejected",
  cancelled: "Cancelled",
};

function describeTool(tool: string, args: Record<string, any>): string {
  switch (tool) {
    case "read_file":
      return args.start_line ? `${args.path} (lines ${args.start_line}–${args.end_line ?? "end"})` : `${args.path}`;
    case "write_file":
    case "edit_file":
    case "delete_file":
      return `${args.path}`;
    case "list_files":
      return !args.path || args.path === "." ? "" : `${args.path}`;
    case "search_code":
      return `"${args.pattern}"`;
    case "execute_command":
      return `${args.command}`;
    case "run_tests":
      return args.command ? `${args.command}` : "the task's test command";
    case "install_dependency":
      return args.requirements_file
        ? `${args.manager} -r ${args.requirements_file}`
        : `${args.manager} ${(args.packages ?? []).join(" ")}`.trim();
    case "create_checkpoint":
      return `${args.label}`;
    case "restore_checkpoint":
      return `${args.checkpoint_id}`;
    case "inspect_git_diff":
      return args.path ? `${args.path}` : args.stat_only ? "summary" : "all changes";
    default:
      return "";
  }
}

/** Apply one event. Mutates and returns `view`; callers copy when they need a new reference. */
export function applyEvent(view: RunView, event: RunEvent): RunView {
  if (event.id <= view.lastEventId) return view; // replays and reconnects resend events
  view.lastEventId = event.id;
  const p = event.payload;
  const push = (item: Omit<TimelineItem, "id" | "at">) =>
    view.timeline.push({ id: event.id, at: event.at, ...item });

  switch (event.type) {
    case "task_created":
      push({ kind: "info", title: "Task created" });
      break;
    case "state_changed": {
      const to = p.to as TaskState;
      view.state = to;
      view.visited.push(to);
      view.error = p.error ?? (to === "failed" || to === "cancelled" ? view.error : null);
      view.activeAgent = null;
      push({ kind: "state", title: STATE_TITLES[to] ?? to, detail: p.error ?? undefined });
      break;
    }
    case "agent_started":
      view.activeAgent = p.agent;
      push({ kind: "agent", agent: p.agent, title: `${label(p.agent)} started`, detail: `up to ${p.max_steps} steps · tools: ${(p.tools ?? []).join(", ")}` });
      break;
    case "agent_finished":
      view.activeAgent = null;
      push({ kind: "agent", agent: p.agent, title: `${label(p.agent)} finished in ${p.steps} step${p.steps === 1 ? "" : "s"}`, detail: summarise(p.output), ok: true });
      break;
    case "agent_note":
      push({ kind: "note", agent: p.agent, title: p.text });
      break;
    case "llm_call":
      if (p.budget) view.usage = p.budget;
      break;
    case "llm_retry":
      push({ kind: "retry", agent: p.agent, title: `Model unavailable — waiting ${p.wait_seconds}s, then trying again`, detail: p.error });
      break;
    case "contract_rejected":
      push({ kind: "error", agent: p.agent, title: "Result did not match its contract; sent back to the agent", detail: p.problems, ok: false });
      break;
    case "tool_call":
      push({ kind: "tool", agent: p.agent, title: p.tool, detail: p.ok ? describeTool(p.tool, p.args ?? {}) : `${describeTool(p.tool, p.args ?? {})} — refused: ${p.error?.code}: ${p.error?.message}`, ok: p.ok });
      break;
    case "file_changed":
      view.touched[p.path] = p.action;
      break;
    case "plan_ready":
      view.plan = p.plan;
      break;
    case "plan_approved":
      push({ kind: "info", title: "Plan approved" });
      break;
    case "plan_rejected":
      push({ kind: "info", title: p.feedback ? "Plan sent back with feedback" : "Plan rejected", detail: p.feedback || undefined });
      break;
    case "sandbox_ready":
      view.isolation = p as Isolation;
      view.sandboxAlive = true;
      push({ kind: "info", title: `Sandbox ready (${p.driver})`, detail: (p.layers ?? []).join(" · ") });
      break;
    case "sandbox_stats":
      view.stats = p;
      break;
    case "sandbox_destroyed":
      view.sandboxAlive = false;
      view.stats = null;
      push({ kind: "info", title: "Sandbox destroyed" });
      break;
    case "command_started":
      view.runs.push({ runId: p.run_id, agent: p.agent, kind: p.kind, command: p.command, network: !!p.network, output: "", exitCode: null, timedOut: false, durationMs: null, startedAt: event.at });
      break;
    case "command_output": {
      const run = view.runs.find((r) => r.runId === p.run_id);
      if (run) run.output += p.text;
      break;
    }
    case "command_finished": {
      const run = view.runs.find((r) => r.runId === p.run_id);
      if (run) {
        run.exitCode = p.exit_code;
        run.timedOut = p.timed_out;
        run.durationMs = p.duration_ms;
      }
      break;
    }
    case "test_result":
      view.tests.push({ at: event.at, agent: p.agent, kind: p.kind, command: p.command, passed: p.passed, summary: p.summary ?? {} });
      push({ kind: "test", agent: p.agent, title: p.kind === "verify" ? (p.passed ? "Verification passed" : "Verification failed") : p.passed ? "Tests passed" : "Tests failed", detail: `${p.command}${formatCounts(p.summary)}`, ok: p.passed });
      break;
    case "checkpoint":
      push({ kind: "checkpoint", agent: p.agent, title: `Checkpoint saved: ${p.label}`, detail: p.id });
      break;
    case "checkpoint_restored":
      push({ kind: "checkpoint", agent: p.agent, title: `Rolled back to checkpoint: ${p.label}`, detail: p.id });
      break;
    case "review_ready":
      view.review = p.review;
      break;
    case "result_ready":
      view.changes = p.changes;
      push({ kind: "info", title: `Result archived (${p.changes?.length ?? 0} files changed)` });
      break;
    case "error":
      push({ kind: "error", title: p.message, ok: false });
      break;
  }
  return view;
}

export function reduceEvents(events: RunEvent[]): RunView {
  const view = emptyView();
  for (const event of events) applyEvent(view, event);
  return view;
}

export function label(agent: string): string {
  return agent.charAt(0).toUpperCase() + agent.slice(1);
}

export function formatCounts(summary: Record<string, number> | undefined): string {
  const parts = Object.entries(summary ?? {}).map(([name, count]) => `${count} ${name}`);
  return parts.length ? ` — ${parts.join(", ")}` : "";
}

function summarise(output: Record<string, any> | undefined): string | undefined {
  if (!output) return undefined;
  const text = output.summary ?? output.fix_summary ?? output.root_cause;
  return typeof text === "string" ? text : undefined;
}
