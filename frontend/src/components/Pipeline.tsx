import type { RunView } from "../lib/reduce";
import type { TaskState } from "../lib/types";
import { cx } from "./ui";

const STAGES: { state: TaskState; label: string }[] = [
  { state: "planning", label: "Plan" },
  { state: "awaiting_approval", label: "Approve" },
  { state: "provisioning", label: "Sandbox" },
  { state: "coding", label: "Code" },
  { state: "executing", label: "Run" },
  { state: "verifying", label: "Verify" },
  { state: "debugging", label: "Debug" },
  { state: "reviewing", label: "Review" },
  { state: "completed", label: "Done" },
];

/** The state machine as a strip: where the task is, where it has been, and how often. */
export function Pipeline({ view }: { view: RunView }) {
  const counts = new Map<TaskState, number>();
  for (const state of view.visited) counts.set(state, (counts.get(state) ?? 0) + 1);
  const ended = ["failed", "cancelled", "rejected"].includes(view.state);
  return (
    <ol className="flex items-center gap-1 overflow-x-auto py-1" aria-label="Task stages">
      {STAGES.map((stage, index) => {
        const visits = counts.get(stage.state) ?? 0;
        const current = view.state === stage.state;
        const done = visits > 0 && !current;
        const skipped = stage.state === "debugging" && visits === 0;
        return (
          <li key={stage.state} className="flex items-center gap-1">
            {index > 0 && <span className={cx("h-px w-4", done || current ? "bg-accent/60" : "bg-line-strong")} />}
            <span
              className={cx(
                "flex items-center gap-1.5 whitespace-nowrap rounded-md border px-2 py-1 text-xs",
                current && !ended && "border-accent bg-accent/10 font-semibold text-accent",
                current && ended && "border-bad/50 text-bad",
                done && "border-line-strong text-text",
                !current && !done && "border-line text-faint",
                skipped && view.state === "completed" && "opacity-50",
              )}
              aria-current={current ? "step" : undefined}
            >
              {current && !ended && stage.state !== "completed" && stage.state !== "awaiting_approval" && (
                <span className="live-dot h-1.5 w-1.5 rounded-full bg-accent" />
              )}
              {done && <span className="text-ok">✓</span>}
              {stage.label}
              {visits > 1 && <span className="rounded bg-raised px-1 font-mono text-[10px] text-muted">×{visits}</span>}
            </span>
          </li>
        );
      })}
      {ended && (
        <li className="flex items-center gap-1">
          <span className="h-px w-4 bg-bad/50" />
          <span className="rounded-md border border-bad/50 bg-bad/10 px-2 py-1 text-xs font-semibold capitalize text-bad">{view.state}</span>
        </li>
      )}
    </ol>
  );
}
