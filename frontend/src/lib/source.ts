// Where a run's data comes from. `live` talks to the API; `replay` reads an exported run
// from a static file. The pages only know this interface.

import type {
  Change,
  Execution,
  FileEntry,
  Project,
  RunBundle,
  RunEvent,
  RunIndexEntry,
  Task,
} from "./types";

export const DEMO = import.meta.env.VITE_DEMO === "1";
const API = (import.meta.env.VITE_API_URL as string | undefined) ?? "";
const TOKEN_KEY = "codepilot_token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* private mode: the token lasts for this page only */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`${API}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Can't reach the CodePilot API. Is it running?");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export interface Health {
  status: string;
  sandbox: string;
  llm: { provider: string; models: string[]; configured: boolean };
  auth_required: boolean;
}

export const api = {
  health: () => request<Health>("/api/health"),
  projects: () => request<Project[]>("/api/projects"),
  project: (id: string) => request<Project>(`/api/projects/${id}`),
  createProject: (name: string, description: string, archive: File | null) => {
    const form = new FormData();
    form.set("name", name);
    form.set("description", description);
    if (archive) form.set("archive", archive);
    return request<Project>("/api/projects", { method: "POST", body: form });
  },
  deleteProject: (id: string) => request<void>(`/api/projects/${id}`, { method: "DELETE" }),
  createTask: (projectId: string, description: string) =>
    request<Task>(`/api/projects/${projectId}/tasks`, { method: "POST", body: JSON.stringify({ description }) }),
  task: (id: string) => request<Task>(`/api/tasks/${id}`),
  approve: (id: string) => request<Task>(`/api/tasks/${id}/approve`, { method: "POST" }),
  reject: (id: string, feedback: string) =>
    request<Task>(`/api/tasks/${id}/reject`, { method: "POST", body: JSON.stringify({ feedback }) }),
  cancel: (id: string) => request<Task>(`/api/tasks/${id}/cancel`, { method: "POST" }),
  files: (id: string) => request<{ files: FileEntry[] }>(`/api/tasks/${id}/files`),
  file: (id: string, path: string, version: "current" | "base") =>
    request<{ content: string; missing?: boolean }>(
      `/api/tasks/${id}/file?path=${encodeURIComponent(path)}&version=${version}`,
    ),
  diff: (id: string) => request<{ live: boolean; changes: Change[]; diff: string }>(`/api/tasks/${id}/diff`),
  executions: (id: string) => request<Execution[]>(`/api/tasks/${id}/executions`),
  downloadUrl: (id: string) => {
    const token = getToken();
    return `${API}/api/tasks/${id}/download${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  },
  streamUrl: (id: string, after: number) => {
    const token = getToken();
    return `${API}/api/tasks/${id}/stream?after=${after}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  },
};

/** Follow a task's events. EventSource reconnects by itself and resends Last-Event-ID. */
export function followEvents(
  taskId: string,
  after: number,
  onEvent: (event: RunEvent) => void,
  onEnd: () => void,
  onStatus: (connected: boolean) => void,
): () => void {
  const source = new EventSource(api.streamUrl(taskId, after));
  source.onopen = () => onStatus(true);
  source.onerror = () => onStatus(false);
  source.onmessage = (message) => onEvent(JSON.parse(message.data) as RunEvent);
  // The server names each event by its type; listen for every type the reducer knows.
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (message) => onEvent(JSON.parse((message as MessageEvent).data) as RunEvent));
  }
  source.addEventListener("end", () => {
    source.close();
    onEnd();
  });
  return () => source.close();
}

const EVENT_TYPES = [
  "task_created", "state_changed", "agent_started", "agent_finished", "agent_note", "llm_call",
  "llm_retry", "contract_rejected", "tool_call", "file_changed", "plan_ready", "plan_approved",
  "plan_rejected", "sandbox_ready", "sandbox_stats", "sandbox_destroyed", "command_started",
  "command_output", "command_finished", "test_result", "checkpoint", "checkpoint_restored",
  "review_ready", "result_ready", "error",
];

// --- replay ---------------------------------------------------------------------------------

export const replay = {
  index: async () => (await fetch("/runs/index.json")).json() as Promise<RunIndexEntry[]>,
  bundle: async (id: string) => {
    const response = await fetch(`/runs/${encodeURIComponent(id)}.json`);
    if (!response.ok) throw new ApiError(response.status, "This recorded run doesn't exist.");
    return (await response.json()) as RunBundle;
  },
};

/** Delay before each event on replay: real gaps, but long waits (model calls, installs) are
 * capped so a 6-minute run plays in about a minute at 1×. */
export function replayDelays(events: RunEvent[], maxGapMs = 900): number[] {
  return events.map((event, index) => {
    if (index === 0) return 0;
    const gap = Date.parse(event.at) - Date.parse(events[index - 1]!.at);
    const floor = event.type === "command_output" ? 30 : 60;
    return Math.min(Math.max(gap, floor), maxGapMs);
  });
}
