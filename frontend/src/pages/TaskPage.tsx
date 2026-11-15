import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChangesPanel, FilesPanel } from "../components/CodePanels";
import { Pipeline } from "../components/Pipeline";
import { PlanPanel } from "../components/PlanPanel";
import { ReplayBar } from "../components/ReplayBar";
import { ReviewPanel, SandboxPanel } from "../components/ReportPanels";
import { TerminalPanel } from "../components/TerminalPanel";
import { Timeline } from "../components/Timeline";
import { Button, cx, Empty, formatDuration, formatTokens, StatePill } from "../components/ui";
import { api } from "../lib/source";
import { TERMINAL } from "../lib/types";
import { useRun } from "../lib/useRun";

type Tab = "plan" | "terminal" | "files" | "changes" | "review" | "sandbox";

export function TaskPage() {
  const { taskId = "" } = useParams();
  const run = useRun(taskId);
  const { task, view } = run;
  const [tab, setTab] = useState<Tab>("plan");
  const [followStage, setFollowStage] = useState(true);
  const [cancelling, setCancelling] = useState(false);

  // Until the user picks a tab, show the one that matches what the run is doing.
  useEffect(() => {
    if (!followStage) return;
    const byState: Partial<Record<string, Tab>> = {
      planning: "plan", awaiting_approval: "plan", coding: "files", executing: "terminal",
      verifying: "terminal", debugging: "terminal", reviewing: "changes", completed: "review", failed: "review",
    };
    const next = byState[view.state];
    if (next) setTab(next);
  }, [view.state, followStage]);

  if (run.error && !task) {
    return (
      <Empty>
        <span>
          {run.error} <Link className="text-accent underline" to="/">Back to projects</Link>
        </span>
      </Empty>
    );
  }
  if (!task) return <Empty>Loading…</Empty>;

  const state = run.demo ? view.state : task.state;
  const usage = Object.keys(view.usage).length ? view.usage : task.usage;
  const plan = view.plan ?? task.plan;
  const review = view.review ?? task.reports.review ?? null;
  const running = !TERMINAL.includes(state);
  const changeCount = (view.changes ?? task.changes)?.length ?? Object.keys(view.touched).length;
  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "plan", label: "Plan" },
    { id: "terminal", label: "Terminal", count: view.runs.length || undefined },
    { id: "files", label: "Files" },
    { id: "changes", label: "Changes", count: changeCount || undefined },
    { id: "review", label: "Review" },
    { id: "sandbox", label: "Sandbox & budget" },
  ];

  return (
    <div className="flex h-full flex-col overflow-x-hidden">
      {run.playback && <ReplayBar playback={run.playback} />}
      <div className="border-b border-line bg-panel px-4 py-3">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-xs text-faint">
              <Link to="/" className="hover:text-text">
                {run.demo ? "Recorded runs" : "Projects"}
              </Link>
              <span>/</span>
              <span className="font-mono">{task.id}</span>
              {!run.demo && running && (
                <span className={cx("ml-1", run.connected ? "text-ok" : "text-warn")}>{run.connected ? "● live" : "○ reconnecting…"}</span>
              )}
            </div>
            <p className="mt-1 line-clamp-2 text-[15px] font-medium leading-snug" title={task.description}>
              {task.description}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {usage.llm_calls !== undefined && (
              <span className="hidden font-mono text-xs text-muted md:inline" title="Model calls · tokens · elapsed">
                {usage.llm_calls} calls · {formatTokens(usage.total_tokens ?? 0)} tok · {formatDuration(usage.elapsed_seconds ?? 0)}
              </span>
            )}
            <StatePill state={state} />
            {!run.demo && running && (
              <Button
                variant="danger"
                disabled={cancelling}
                onClick={async () => {
                  setCancelling(true);
                  try {
                    await api.cancel(task.id);
                    await run.refresh();
                  } finally {
                    setCancelling(false);
                  }
                }}
              >
                Cancel
              </Button>
            )}
            {!run.demo && task.has_result && (
              <a
                href={api.downloadUrl(task.id)}
                className="inline-flex items-center rounded-md bg-accent px-3 py-1.5 text-[13px] font-semibold text-accent-ink hover:brightness-110"
              >
                Download ZIP
              </a>
            )}
          </div>
        </div>
        <div className="mt-2.5">
          <Pipeline view={view} />
        </div>
        {(view.error ?? task.error) && !running && state !== "completed" && (
          <p className="mt-2 rounded border border-bad/30 bg-bad/5 px-3 py-1.5 text-[13px] text-bad">{view.error ?? task.error}</p>
        )}
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(300px,380px)_minmax(0,1fr)]">
        <aside className="flex min-h-0 flex-col border-b border-line max-lg:h-64 lg:border-b-0 lg:border-r">
          <h2 className="flex items-center justify-between border-b border-line px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
            Activity
            {view.activeAgent && (
              <span className="flex items-center gap-1.5 normal-case tracking-normal text-accent">
                <span className="live-dot h-1.5 w-1.5 rounded-full bg-accent" />
                {view.activeAgent} is working
              </span>
            )}
          </h2>
          <div className="min-h-0 flex-1">
            <Timeline view={view} />
          </div>
        </aside>

        <section className="flex min-h-0 min-w-0 flex-col">
          <div role="tablist" className="flex shrink-0 gap-1 overflow-x-auto border-b border-line bg-panel px-2">
            {tabs.map((item) => (
              <button
                key={item.id}
                role="tab"
                type="button"
                aria-selected={tab === item.id}
                onClick={() => {
                  setTab(item.id);
                  setFollowStage(false);
                }}
                className={cx(
                  "flex items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-[13px]",
                  tab === item.id ? "border-accent text-text" : "border-transparent text-muted hover:text-text",
                )}
              >
                {item.label}
                {item.count !== undefined && <span className="rounded bg-raised px-1.5 font-mono text-[10.5px] text-muted">{item.count}</span>}
              </button>
            ))}
          </div>
          <div className="min-h-0 flex-1" role="tabpanel">
            {tab === "plan" && <PlanPanel task={{ ...task, state }} plan={plan} demo={run.demo} onChanged={() => void run.refresh()} />}
            {tab === "terminal" && <TerminalPanel view={view} />}
            {tab === "files" && <FilesPanel files={run.files} version={run.filesVersion} touched={view.touched} />}
            {tab === "changes" && <ChangesPanel files={run.files} version={run.filesVersion} />}
            {tab === "review" && <ReviewPanel task={task} review={review} view={view} />}
            {tab === "sandbox" && <SandboxPanel view={view} usage={usage} />}
          </div>
        </section>
      </div>
    </div>
  );
}
