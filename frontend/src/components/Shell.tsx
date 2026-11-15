import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { DEMO } from "../lib/source";

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-line bg-panel px-4">
        <Link to="/" className="flex items-center gap-2.5 font-semibold tracking-tight">
          <svg viewBox="0 0 32 32" className="h-6 w-6" aria-hidden="true">
            <rect width="32" height="32" rx="7" fill="#0c1017" stroke="#222a38" />
            <path d="M12 10l-6 6 6 6M20 10l6 6-6 6" fill="none" stroke="#5eead4" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          CodePilot
          {DEMO && (
            <span className="rounded border border-line-strong px-1.5 py-0.5 text-[11px] font-medium text-muted">
              recorded runs
            </span>
          )}
        </Link>
        <nav className="flex items-center gap-4 text-[13px] text-muted">
          <a className="hover:text-text" href="https://github.com/iampdt/codepilot" target="_blank" rel="noreferrer">
            Source
          </a>
          <a className="hover:text-text" href="https://github.com/iampdt/codepilot/blob/main/docs/threat-model.md" target="_blank" rel="noreferrer">
            Threat model
          </a>
        </nav>
      </header>
      <main className="min-h-0 flex-1">{children}</main>
    </div>
  );
}
