import { useEffect, useRef } from "react";
import type { RunView, TerminalRun } from "../lib/reduce";
import { AgentTag, cx, Empty } from "./ui";

const KIND_LABEL: Record<string, string> = { command: "run", install: "install", test: "test", verify: "verify" };

function RunBlock({ run }: { run: TerminalRun }) {
  const running = run.exitCode === null;
  const ok = run.exitCode === 0 && !run.timedOut;
  return (
    <div className="border-b border-line/60 px-4 py-3 font-mono text-[12.5px] leading-relaxed">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <AgentTag agent={run.agent} />
        <span className="rounded border border-line-strong px-1 text-[10px] uppercase tracking-wider text-muted">{KIND_LABEL[run.kind] ?? run.kind}</span>
        {run.network && (
          <span className="rounded border border-warn/40 px-1 text-[10px] uppercase tracking-wider text-warn" title="Attached to the registry-only proxy for this command">
            registry network
          </span>
        )}
        <span className="ml-auto text-[11px]">
          {running ? (
            <span className="text-info">
              <span className="live-dot mr-1 inline-block h-1.5 w-1.5 rounded-full bg-info" />
              running
            </span>
          ) : (
            <span className={ok ? "text-ok" : "text-bad"}>
              {run.timedOut ? "timed out" : `exit ${run.exitCode}`}
              <span className="ml-2 text-faint">{((run.durationMs ?? 0) / 1000).toFixed(1)}s</span>
            </span>
          )}
        </span>
      </div>
      <p className="mt-1.5 break-all text-text">
        <span className="select-none text-accent">$ </span>
        {run.command}
      </p>
      {run.output && <pre className={cx("mt-1.5 max-h-96 overflow-y-auto whitespace-pre-wrap text-[12px] [overflow-wrap:anywhere]", ok || running ? "text-muted" : "text-[#e8b4b4]")}>{run.output}</pre>}
    </div>
  );
}

export function TerminalPanel({ view }: { view: RunView }) {
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const size = view.runs.reduce((total, run) => total + run.output.length, view.runs.length);
  useEffect(() => {
    if (pinned.current) end.current?.scrollIntoView({ block: "end" });
  }, [size]);
  if (view.runs.length === 0) return <Empty>No commands yet. Every command the agents run in the sandbox appears here with its output and exit code.</Empty>;
  return (
    <div
      ref={box}
      className="h-full overflow-y-auto bg-[#080a0f]"
      onScroll={() => {
        const el = box.current;
        if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
    >
      {view.runs.map((run) => (
        <RunBlock key={run.runId} run={run} />
      ))}
      <div ref={end} />
    </div>
  );
}
