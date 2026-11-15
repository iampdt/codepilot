import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Button, Empty, formatDuration, Panel, StatePill } from "../components/ui";
import { api, ApiError, DEMO, getToken, replay, setToken, type Health } from "../lib/source";
import type { Project, RunIndexEntry } from "../lib/types";

const EXAMPLES = [
  "Create a FastAPI application with JWT authentication, user registration, login and logout, SQLAlchemy models that work with PostgreSQL in production and SQLite in tests, and unit tests.",
  "Build a command-line todo manager in Python with add, list, complete and delete commands, JSON file storage, and pytest tests.",
  "Write a rate limiter library (token bucket and sliding window) with a clean API and thorough unit tests.",
];

function DemoHome() {
  const [runs, setRuns] = useState<RunIndexEntry[] | null>(null);
  useEffect(() => {
    replay.index().then(setRuns).catch(() => setRuns([]));
  }, []);
  return (
    <div className="mx-auto h-full max-w-4xl overflow-y-auto px-5 py-10">
      <h1 className="text-3xl font-semibold tracking-tight">An autonomous coding agent you can watch work</h1>
      <p className="mt-3 max-w-2xl text-[15px] leading-relaxed text-muted">
        CodePilot takes a task in plain English, plans it, waits for approval, then writes the code, runs it and fixes its own
        failures inside a locked-down container. Five agents with separate permissions; one state machine in charge; a verdict
        that comes from running the tests, not from asking the model.
      </p>
      <p className="mt-3 max-w-2xl rounded-md border border-line bg-panel px-4 py-3 text-[13px] leading-relaxed text-muted">
        <span className="font-semibold text-text">These are recordings of real runs.</span> The sandbox needs a Docker host, which no
        free hosting tier provides, so this page replays the exact event logs the system produced — same UI, same data. To run it
        live, clone the repository and <code className="font-mono text-accent">docker compose up</code>.
      </p>
      <h2 className="mb-3 mt-8 text-[11px] font-semibold uppercase tracking-wider text-muted">Recorded runs</h2>
      {runs === null ? (
        <Empty>Loading…</Empty>
      ) : runs.length === 0 ? (
        <Empty>No recordings are published yet.</Empty>
      ) : (
        <ul className="space-y-3">
          {runs.map((run) => (
            <li key={run.id}>
              <Link to={`/runs/${run.id}`} className="block rounded-lg border border-line bg-panel p-4 transition hover:border-accent/60">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-[15px] font-semibold">{run.title}</h3>
                  <StatePill state={run.outcome} />
                  <span className="ml-auto font-mono text-xs text-faint">
                    {run.model} · {formatDuration(run.duration_seconds)}
                  </span>
                </div>
                <p className="mt-1.5 text-[13px] text-muted">{run.description}</p>
                {run.highlights.length > 0 && (
                  <ul className="mt-2.5 flex flex-wrap gap-1.5">
                    {run.highlights.map((highlight) => (
                      <li key={highlight} className="rounded border border-line-strong px-1.5 py-0.5 text-[11px] text-muted">
                        {highlight}
                      </li>
                    ))}
                  </ul>
                )}
                <p className="mt-3 text-[13px] font-medium text-accent">▶ Watch the run</p>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function LiveHome() {
  const navigate = useNavigate();
  const [health, setHealth] = useState<Health | null>(null);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState(false);
  const [name, setName] = useState("");
  const [archive, setArchive] = useState<File | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setHealth(await api.health());
      const list = await api.projects();
      setProjects(list);
      setSelected((current) => current ?? list[0]?.id ?? null);
      setError(null);
      setNeedsToken(false);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) setNeedsToken(true);
      else setError((cause as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const guard = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (needsToken) {
    return (
      <div className="mx-auto max-w-sm px-5 py-16">
        <Panel title="Access token">
          <form
            className="space-y-3 p-4"
            onSubmit={(e) => {
              e.preventDefault();
              setToken(String(new FormData(e.currentTarget).get("token") ?? ""));
              void load();
            }}
          >
            <p className="text-[13px] text-muted">This instance requires its API token.</p>
            <input name="token" type="password" defaultValue={getToken()} autoFocus className="w-full rounded-md border border-line-strong bg-ink px-3 py-2 text-[13px]" />
            <Button variant="primary" type="submit">
              Continue
            </Button>
          </form>
        </Panel>
      </div>
    );
  }

  const project = projects?.find((p) => p.id === selected) ?? null;
  return (
    <div className="mx-auto grid h-full max-w-6xl gap-5 overflow-y-auto px-5 py-6 lg:grid-cols-[300px_1fr]">
      <div className="space-y-4">
        <Panel title="Projects">
          {projects === null ? (
            <Empty>{error ?? "Loading…"}</Empty>
          ) : projects.length === 0 ? (
            <Empty>No projects yet. Create one below.</Empty>
          ) : (
            <ul className="max-h-72 overflow-y-auto py-1">
              {projects.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(p.id)}
                    className={`flex w-full items-center justify-between px-4 py-2 text-left text-[13px] hover:bg-raised ${p.id === selected ? "bg-raised text-accent" : ""}`}
                  >
                    <span className="truncate">{p.name}</span>
                    <span className="font-mono text-[11px] text-faint">{p.tasks?.length ?? 0}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel title="New project">
          <form
            className="space-y-3 p-4"
            onSubmit={(e) => {
              e.preventDefault();
              void guard(async () => {
                const created = await api.createProject(name, "", archive);
                setName("");
                setArchive(null);
                (e.target as HTMLFormElement).reset();
                await load();
                setSelected(created.id);
              });
            }}
          >
            <input required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} placeholder="Project name" className="w-full rounded-md border border-line-strong bg-ink px-3 py-2 text-[13px] placeholder:text-faint" />
            <label className="block text-xs text-muted">
              Existing codebase (optional .zip)
              <input type="file" accept=".zip,application/zip" onChange={(e) => setArchive(e.target.files?.[0] ?? null)} className="mt-1 block w-full text-xs text-muted file:mr-2 file:rounded file:border file:border-line-strong file:bg-raised file:px-2 file:py-1 file:text-text" />
            </label>
            <p className="text-xs text-faint">Leave empty to start from an empty repository. Uploads are treated as untrusted: checked for path traversal, links and zip bombs, and only ever opened inside a sandbox.</p>
            <Button variant="secondary" type="submit" disabled={busy || !name.trim()}>
              Create project
            </Button>
          </form>
        </Panel>
        {health && (
          <p className="px-1 text-xs text-faint">
            Sandbox: {health.sandbox} · Model: {health.llm.configured ? health.llm.models[0] : <span className="text-warn">not configured — set GEMINI_API_KEY</span>}
          </p>
        )}
      </div>

      <div className="space-y-4">
        {error && <p className="rounded border border-bad/30 bg-bad/5 px-3 py-2 text-[13px] text-bad">{error}</p>}
        {project ? (
          <>
            <Panel
              title={`New task in “${project.name}”`}
              action={
                <Button variant="ghost" className="!py-0.5 !text-xs" disabled={busy} onClick={() => guard(async () => { await api.deleteProject(project.id); setSelected(null); await load(); })}>
                  Delete project
                </Button>
              }
            >
              <form
                className="space-y-3 p-4"
                onSubmit={(e) => {
                  e.preventDefault();
                  void guard(async () => {
                    const task = await api.createTask(project.id, description);
                    navigate(`/tasks/${task.id}`);
                  });
                }}
              >
                <textarea
                  required
                  minLength={10}
                  maxLength={6000}
                  rows={5}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Describe what to build or change…"
                  className="w-full resize-y rounded-md border border-line-strong bg-ink px-3 py-2 text-[14px] leading-relaxed placeholder:text-faint"
                />
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="primary" type="submit" disabled={busy || description.trim().length < 10}>
                    Plan this task
                  </Button>
                  <span className="text-xs text-faint">You approve the plan before any code is written or run.</span>
                </div>
                <div>
                  <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">Examples</p>
                  <ul className="space-y-1">
                    {EXAMPLES.map((example) => (
                      <li key={example}>
                        <button type="button" className="text-left text-xs text-muted hover:text-accent" onClick={() => setDescription(example)}>
                          → {example}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              </form>
            </Panel>
            <Panel title="Tasks">
              {(project.tasks?.length ?? 0) === 0 ? (
                <Empty>No tasks in this project yet.</Empty>
              ) : (
                <ul className="divide-y divide-line">
                  {project.tasks!.map((task) => (
                    <li key={task.id}>
                      <Link to={`/tasks/${task.id}`} className="flex items-center gap-3 px-4 py-2.5 hover:bg-raised">
                        <span className="min-w-0 flex-1 truncate text-[13px]">{task.description}</span>
                        <span className="hidden font-mono text-[11px] text-faint sm:inline">{new Date(task.created_at).toLocaleString()}</span>
                        <StatePill state={task.state} />
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </>
        ) : (
          <Empty>Create a project to give the agent somewhere to work.</Empty>
        )}
      </div>
    </div>
  );
}

export function Home() {
  return DEMO ? <DemoHome /> : <LiveHome />;
}
