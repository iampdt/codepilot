import type { RunView } from "../lib/reduce";
import { formatCounts } from "../lib/reduce";
import type { Review, Task, Usage } from "../lib/types";
import { AgentTag, cx, Empty, formatDuration, formatTokens } from "./ui";

const SEVERITY: Record<string, string> = {
  blocking: "border-bad/50 text-bad",
  major: "border-warn/50 text-warn",
  minor: "border-line-strong text-muted",
  info: "border-line-strong text-faint",
};

export function ReviewPanel({ task, review, view }: { task: Task; review: Review | null; view: RunView }) {
  const verification = [...view.tests].reverse().find((t) => t.kind === "verify");
  const debugging = task.reports.debugging ?? [];
  if (!review && !verification) return <Empty>The review report appears here once the tests have been verified.</Empty>;
  return (
    <div className="h-full space-y-5 overflow-y-auto p-5">
      {verification && (
        <div className={cx("rounded-md border p-3", verification.passed ? "border-ok/30 bg-ok/5" : "border-bad/30 bg-bad/5")}>
          <p className={cx("text-[13px] font-semibold", verification.passed ? "text-ok" : "text-bad")}>
            {verification.passed ? "Verified: tests pass" : "Verification failed"}
            <span className="font-normal text-muted">{formatCounts(verification.summary)}</span>
          </p>
          <p className="mt-1 text-xs text-muted">
            Run by the orchestrator itself (<code className="font-mono">{verification.command}</code>), not taken from an agent's report.
          </p>
        </div>
      )}
      {debugging.length > 0 && (
        <div>
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Debugging</h3>
          <ol className="space-y-2">
            {debugging.map((attempt) => (
              <li key={attempt.attempt} className="rounded-md border border-line bg-raised/50 p-3 text-[13px]">
                <p className="text-xs text-violet">Attempt {attempt.attempt}</p>
                <p className="mt-1">
                  <span className="text-muted">Root cause: </span>
                  {attempt.root_cause}
                </p>
                <p className="mt-1">
                  <span className="text-muted">Fix: </span>
                  {attempt.fix_summary}
                </p>
              </li>
            ))}
          </ol>
        </div>
      )}
      {review && (
        <>
          <div>
            <div className="flex items-center gap-2">
              <span className={cx("rounded-full border px-2.5 py-0.5 text-xs font-semibold", review.verdict === "approve" ? "border-ok/40 text-ok" : "border-warn/50 text-warn")}>
                {review.verdict === "approve" ? "Approved" : "Changes requested"}
              </span>
              {task.review_rounds > 0 && <span className="text-xs text-faint">after {task.review_rounds} revision round</span>}
            </div>
            <p className="mt-3 text-[14px] leading-relaxed">{review.summary}</p>
          </div>
          {review.criteria.length > 0 && (
            <div>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Acceptance criteria</h3>
              <ul className="space-y-1.5">
                {review.criteria.map((check, index) => (
                  <li key={index} className="flex gap-2 text-[13px]">
                    <span className={check.met ? "text-ok" : "text-bad"}>{check.met ? "✓" : "✗"}</span>
                    <span>
                      {check.criterion}
                      {check.evidence && <span className="block text-xs text-muted">{check.evidence}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {review.findings.length > 0 && (
            <div>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Findings</h3>
              <ul className="space-y-2">
                {review.findings.map((finding, index) => (
                  <li key={index} className="rounded-md border border-line bg-raised/50 p-3 text-[13px]">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={cx("rounded border px-1.5 text-[10px] font-semibold uppercase tracking-wider", SEVERITY[finding.severity])}>{finding.severity}</span>
                      {finding.file && <code className="font-mono text-[11px] text-info">{finding.file}</code>}
                    </div>
                    <p className="mt-1.5">{finding.issue}</p>
                    {finding.suggestion && <p className="mt-1 text-muted">→ {finding.suggestion}</p>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {review.security_notes && (
            <div>
              <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">Security notes</h3>
              <p className="text-[13px] text-muted">{review.security_notes}</p>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Meter({ label, used, limit, format = String }: { label: string; used: number; limit: number; format?: (n: number) => string }) {
  const ratio = limit > 0 ? Math.min(used / limit, 1) : 0;
  return (
    <div>
      <div className="flex justify-between text-xs">
        <span className="text-muted">{label}</span>
        <span className="font-mono">
          {format(used)} <span className="text-faint">/ {format(limit)}</span>
        </span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-raised" role="meter" aria-label={label} aria-valuenow={used} aria-valuemin={0} aria-valuemax={limit}>
        <div className={cx("h-full rounded-full", ratio > 0.85 ? "bg-bad" : ratio > 0.6 ? "bg-warn" : "bg-accent")} style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}

export function SandboxPanel({ view, usage }: { view: RunView; usage: Partial<Usage> }) {
  const limits = usage.limits;
  const agents = Object.entries(usage.by_agent ?? {});
  return (
    <div className="grid h-full gap-5 overflow-y-auto p-5 lg:grid-cols-2">
      <div className="space-y-5">
        <div>
          <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Execution budget</h3>
          {limits ? (
            <div className="space-y-3">
              <Meter label="Model calls" used={usage.llm_calls ?? 0} limit={limits.max_llm_calls} />
              <Meter label="Tokens" used={usage.total_tokens ?? 0} limit={limits.max_total_tokens} format={formatTokens} />
              <Meter label="Tool calls" used={usage.tool_calls ?? 0} limit={limits.max_tool_calls} />
              <Meter label="Time" used={usage.elapsed_seconds ?? 0} limit={limits.max_wall_seconds} format={formatDuration} />
            </div>
          ) : (
            <p className="text-[13px] text-faint">No model calls yet.</p>
          )}
          <p className="mt-3 text-xs text-faint">Reaching any ceiling ends the run. Agents can't extend their own budget.</p>
        </div>
        {agents.length > 0 && (
          <div>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">By agent</h3>
            <table className="w-full text-xs">
              <thead className="text-faint">
                <tr>
                  <th className="pb-1 text-left font-normal">Agent</th>
                  <th className="pb-1 text-right font-normal">Model calls</th>
                  <th className="pb-1 text-right font-normal">Tool calls</th>
                  <th className="pb-1 text-right font-normal">Tokens</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {agents.map(([agent, used]) => (
                  <tr key={agent} className="border-t border-line/60">
                    <td className="py-1.5">
                      <AgentTag agent={agent} />
                    </td>
                    <td className="py-1.5 text-right">{used.llm_calls}</td>
                    <td className="py-1.5 text-right">{used.tool_calls}</td>
                    <td className="py-1.5 text-right">{formatTokens(used.input_tokens + used.output_tokens)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="space-y-5">
        <div>
          <h3 className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
            Sandbox
            {view.isolation && (
              <span className={cx("rounded-full border px-2 py-px text-[10px] normal-case tracking-normal", view.sandboxAlive ? "border-ok/40 text-ok" : "border-line-strong text-faint")}>
                {view.sandboxAlive ? "running" : "destroyed"}
              </span>
            )}
          </h3>
          {view.isolation ? (
            <>
              <ul className="space-y-1.5 text-[13px]">
                {view.isolation.layers.map((layer) => (
                  <li key={layer} className="flex gap-2">
                    <span className="text-accent">▪</span>
                    {layer}
                  </li>
                ))}
              </ul>
              <dl className="mt-4 grid grid-cols-3 gap-2 text-xs">
                {Object.entries(view.isolation.limits).map(([name, value]) => (
                  <div key={name} className="rounded border border-line bg-raised/50 px-2 py-1.5">
                    <dt className="text-faint">{name.replace(/_/g, " ")}</dt>
                    <dd className="font-mono text-text">{value}</dd>
                  </div>
                ))}
              </dl>
            </>
          ) : (
            <p className="text-[13px] text-faint">No sandbox exists until the plan is approved. The planner reads the repository from an archive, so nothing can run before then.</p>
          )}
        </div>
        {view.stats && view.sandboxAlive && (
          <div>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Live usage</h3>
            <dl className="grid grid-cols-3 gap-2 text-xs">
              <div className="rounded border border-line bg-raised/50 px-2 py-1.5">
                <dt className="text-faint">CPU</dt>
                <dd className="font-mono">{view.stats.cpu_percent}</dd>
              </div>
              <div className="rounded border border-line bg-raised/50 px-2 py-1.5">
                <dt className="text-faint">Memory</dt>
                <dd className="font-mono">{view.stats.memory}</dd>
              </div>
              <div className="rounded border border-line bg-raised/50 px-2 py-1.5">
                <dt className="text-faint">Processes</dt>
                <dd className="font-mono">{view.stats.pids}</dd>
              </div>
            </dl>
          </div>
        )}
      </div>
    </div>
  );
}
