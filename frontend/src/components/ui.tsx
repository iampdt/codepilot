import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { Agent, TaskState } from "../lib/types";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

const STATE_STYLE: Record<TaskState, { label: string; tone: string; live?: boolean }> = {
  queued: { label: "Queued", tone: "text-muted border-line-strong" },
  planning: { label: "Planning", tone: "text-info border-info/40", live: true },
  awaiting_approval: { label: "Needs approval", tone: "text-warn border-warn/50" },
  provisioning: { label: "Starting sandbox", tone: "text-info border-info/40", live: true },
  coding: { label: "Coding", tone: "text-info border-info/40", live: true },
  executing: { label: "Running", tone: "text-info border-info/40", live: true },
  verifying: { label: "Verifying", tone: "text-info border-info/40", live: true },
  debugging: { label: "Debugging", tone: "text-violet border-violet/40", live: true },
  reviewing: { label: "Reviewing", tone: "text-info border-info/40", live: true },
  completed: { label: "Completed", tone: "text-ok border-ok/40" },
  failed: { label: "Failed", tone: "text-bad border-bad/40" },
  rejected: { label: "Rejected", tone: "text-muted border-line-strong" },
  cancelled: { label: "Cancelled", tone: "text-muted border-line-strong" },
};

export function StatePill({ state }: { state: TaskState }) {
  const style = STATE_STYLE[state];
  return (
    <span className={cx("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium", style.tone)}>
      {style.live && <span className="live-dot h-1.5 w-1.5 rounded-full bg-current" />}
      {style.label}
    </span>
  );
}

const AGENT_TONE: Record<string, string> = {
  planner: "text-info",
  coder: "text-accent",
  executor: "text-warn",
  debugger: "text-violet",
  reviewer: "text-ok",
  orchestrator: "text-muted",
};

export function AgentTag({ agent }: { agent: Agent | string }) {
  return (
    <span className={cx("font-mono text-[11px] uppercase tracking-wider", AGENT_TONE[agent] ?? "text-muted")}>
      {agent}
    </span>
  );
}

export function Button({
  variant = "secondary",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost" }) {
  const styles = {
    primary: "bg-accent text-accent-ink hover:brightness-110 font-semibold",
    secondary: "border border-line-strong bg-raised text-text hover:border-muted",
    danger: "border border-bad/40 text-bad hover:bg-bad/10",
    ghost: "text-muted hover:text-text",
  }[variant];
  return (
    <button
      {...props}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] transition disabled:cursor-not-allowed disabled:opacity-50",
        styles,
        className,
      )}
    />
  );
}

export function Panel({ title, action, children, className }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx("rounded-lg border border-line bg-panel", className)}>
      {title && (
        <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
          <h2 className="text-[13px] font-semibold text-text">{title}</h2>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="flex h-full min-h-32 items-center justify-center p-6 text-center text-[13px] text-faint">{children}</div>;
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function formatTokens(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(2)}M`;
  if (count >= 1000) return `${(count / 1000).toFixed(1)}k`;
  return String(count);
}

export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
