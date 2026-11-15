import { useState } from "react";
import { api } from "../lib/source";
import type { Plan, Task } from "../lib/types";
import { Button, Empty } from "./ui";

function List({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
      <ul className="space-y-1 text-[13px]">
        {items.map((item, index) => (
          <li key={index} className="flex gap-2">
            <span className="text-faint">–</span>
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PlanPanel({ task, plan, demo, onChanged }: { task: Task; plan: Plan | null; demo: boolean; onChanged: () => void }) {
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!plan) return <Empty>The planner is inspecting the repository. The plan appears here for approval before anything runs.</Empty>;
  const waiting = task.state === "awaiting_approval";

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      setFeedback("");
      onChanged();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      {waiting && (
        <div className="sticky top-0 z-10 border-b border-warn/30 bg-[#1d1a10] px-5 py-3">
          <p className="text-[13px] font-semibold text-warn">Approval needed</p>
          <p className="mt-0.5 text-[13px] text-muted">
            Nothing has been executed and no sandbox exists yet. Approve to let the agents implement this plan, or send it back.
          </p>
          {demo ? (
            <p className="mt-2 text-xs text-faint">In this recording the plan was approved as shown.</p>
          ) : (
            <div className="mt-3 flex flex-wrap items-start gap-2">
              <Button variant="primary" disabled={busy} onClick={() => act(() => api.approve(task.id))}>
                Approve plan
              </Button>
              <input
                className="min-w-48 flex-1 rounded-md border border-line-strong bg-ink px-3 py-1.5 text-[13px] placeholder:text-faint"
                placeholder="What should change? (sends the plan back to the planner)"
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                maxLength={3000}
              />
              <Button disabled={busy || !feedback.trim()} onClick={() => act(() => api.reject(task.id, feedback))}>
                Revise
              </Button>
              <Button variant="danger" disabled={busy} onClick={() => act(() => api.reject(task.id, ""))}>
                Reject
              </Button>
            </div>
          )}
          {error && <p className="mt-2 text-xs text-bad">{error}</p>}
        </div>
      )}
      <div className="space-y-5 p-5">
        <div>
          <p className="text-[15px] leading-relaxed">{plan.summary}</p>
          <p className="mt-2 text-[13px] leading-relaxed text-muted">{plan.approach}</p>
          {task.plan_revisions > 0 && <p className="mt-2 text-xs text-faint">Revision {task.plan_revisions} of this plan.</p>}
        </div>
        <div>
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Steps</h3>
          <ol className="space-y-2">
            {plan.steps.map((step) => (
              <li key={step.id} className="rounded-md border border-line bg-raised/50 p-3">
                <div className="flex gap-2.5">
                  <span className="font-mono text-xs text-accent">{String(step.id).padStart(2, "0")}</span>
                  <div className="min-w-0">
                    <p className="text-[13px] font-semibold">{step.title}</p>
                    <p className="mt-1 text-[13px] text-muted">{step.description}</p>
                    {step.files.length > 0 && (
                      <p className="mt-2 flex flex-wrap gap-1.5">
                        {step.files.map((file) => (
                          <code key={file} className="rounded bg-ink px-1.5 py-0.5 font-mono text-[11px] text-info">
                            {file}
                          </code>
                        ))}
                      </p>
                    )}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </div>
        <div className="grid gap-5 md:grid-cols-2">
          <List title="Acceptance criteria" items={plan.acceptance_criteria} />
          <List title="Risks" items={plan.risks} />
        </div>
        <div className="grid gap-5 md:grid-cols-2">
          <div>
            <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">Testing</h3>
            <p className="text-[13px] text-muted">{plan.test_strategy}</p>
            <code className="mt-2 inline-block rounded bg-ink px-2 py-1 font-mono text-xs text-accent">$ {plan.test_command}</code>
          </div>
          {plan.dependencies.length > 0 && (
            <div>
              <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">Dependencies</h3>
              <p className="flex flex-wrap gap-1.5">
                {plan.dependencies.map((dep) => (
                  <code key={dep} className="rounded bg-ink px-1.5 py-0.5 font-mono text-[11px]">
                    {dep}
                  </code>
                ))}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
