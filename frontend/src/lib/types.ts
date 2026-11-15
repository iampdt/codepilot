export type TaskState =
  | "queued"
  | "planning"
  | "awaiting_approval"
  | "provisioning"
  | "coding"
  | "executing"
  | "verifying"
  | "debugging"
  | "reviewing"
  | "completed"
  | "failed"
  | "rejected"
  | "cancelled";

export const TERMINAL: readonly TaskState[] = ["completed", "failed", "rejected", "cancelled"];

export type Agent = "planner" | "coder" | "executor" | "debugger" | "reviewer" | "orchestrator";

export interface PlanStep {
  id: number;
  title: string;
  description: string;
  files: string[];
}

export interface Plan {
  summary: string;
  approach: string;
  steps: PlanStep[];
  acceptance_criteria: string[];
  risks: string[];
  dependencies: string[];
  test_strategy: string;
  test_command: string;
}

export interface ReviewFinding {
  severity: "blocking" | "major" | "minor" | "info";
  file: string;
  issue: string;
  suggestion: string;
}

export interface Review {
  verdict: "approve" | "request_changes";
  summary: string;
  criteria: { criterion: string; met: boolean; evidence: string }[];
  findings: ReviewFinding[];
  security_notes: string;
}

export interface AgentUsage {
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  tool_calls: number;
}

export interface Usage {
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  tool_calls: number;
  elapsed_seconds: number;
  by_agent: Record<string, AgentUsage>;
  limits: {
    max_llm_calls: number;
    max_total_tokens: number;
    max_tool_calls: number;
    max_wall_seconds: number;
  };
}

export interface Change {
  path: string;
  status: "added" | "modified" | "deleted";
  added: number;
  removed: number;
}

export interface Isolation {
  driver: string;
  layers: string[];
  limits: Record<string, number>;
}

export interface TaskBrief {
  id: string;
  project_id: string;
  description: string;
  state: TaskState;
  error: string | null;
  tests_passed: boolean | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Task extends TaskBrief {
  plan: Plan | null;
  plan_revisions: number;
  reports: {
    coding?: { summary: string; files_changed: string[]; test_command: string; notes: string };
    execution?: { summary: string; tests_passed: boolean; failures: string[] };
    verification?: { command: string; passed: boolean; exit_code: number; summary: Record<string, number> };
    debugging?: { attempt: number; root_cause: string; fix_summary: string; tests_passing: boolean }[];
    review?: Review;
  };
  usage: Partial<Usage>;
  debug_iterations: number;
  review_rounds: number;
  test_command: string | null;
  changes: Change[] | null;
  isolation: Isolation | null;
  has_result: boolean;
}

export interface Project {
  id: string;
  name: string;
  description: string;
  created_at: string;
  tasks?: TaskBrief[];
}

export interface RunEvent {
  id: number;
  task_id: string;
  type: string;
  payload: Record<string, any>;
  at: string;
}

export interface Execution {
  id: number;
  agent: string;
  kind: string;
  command: string;
  exit_code: number;
  timed_out: boolean;
  duration_ms: number;
  output: string;
  at: string;
}

export interface FileEntry {
  path: string;
  size: number;
}

/** A finished run exported by the API (`GET /api/tasks/:id/export`), replayed by the demo. */
export interface RunBundle {
  project: Project | null;
  task: Task;
  events: RunEvent[];
  executions: Execution[];
  files: Record<string, string>;
  base_files: Record<string, string>;
  diff: string;
}

export interface RunIndexEntry {
  id: string;
  title: string;
  description: string;
  outcome: TaskState;
  model: string;
  duration_seconds: number;
  highlights: string[];
}
