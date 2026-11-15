import { useEffect, useRef, useState } from "react";
import type { RunView, TimelineItem } from "../lib/reduce";
import { AgentTag, cx, Empty, formatClock } from "./ui";

const ICON: Record<TimelineItem["kind"], string> = {
  state: "◆",
  agent: "●",
  tool: "›",
  note: "“",
  retry: "↻",
  test: "✓",
  checkpoint: "⚑",
  info: "·",
  error: "!",
};

function Row({ item }: { item: TimelineItem }) {
  const [open, setOpen] = useState(false);
  const long = (item.detail?.length ?? 0) > 90 || item.title.length > 160;
  if (item.kind === "state") {
    return (
      <li className="mt-3 flex items-center gap-2 first:mt-0">
        <span className="h-px flex-1 bg-line" />
        <span className={cx("text-[11px] font-semibold uppercase tracking-wider", item.detail ? "text-bad" : "text-muted")}>{item.title}</span>
        <span className="h-px flex-1 bg-line" />
      </li>
    );
  }
  const tone =
    item.ok === false ? "text-bad" : item.kind === "test" ? "text-ok" : item.kind === "retry" ? "text-warn" : "text-faint";
  return (
    <li className="group flex gap-2 py-[3px] text-[12.5px] leading-snug">
      <span className={cx("w-3 shrink-0 text-center font-mono", tone)}>{item.kind === "test" && item.ok === false ? "✗" : ICON[item.kind]}</span>
      <div className="min-w-0 flex-1">
        <button
          type="button"
          className={cx("block w-full text-left", long ? "cursor-pointer" : "cursor-default")}
          onClick={() => long && setOpen((o) => !o)}
          aria-expanded={long ? open : undefined}
        >
          {item.agent && item.kind !== "agent" && (
            <span className="mr-1.5">
              <AgentTag agent={item.agent} />
            </span>
          )}
          <span
            className={cx(
              item.kind === "tool" && "font-mono text-[12px]",
              item.kind === "note" ? "italic text-muted" : item.ok === false ? "text-bad" : "text-text",
              !open && item.kind === "note" && "line-clamp-3",
            )}
          >
            {item.title}
          </span>
          {item.detail && (
            <span
              className={cx(
                "text-muted",
                item.kind === "tool" ? "ml-1.5 font-mono text-[12px] [overflow-wrap:anywhere]" : "mt-0.5 block text-[12px]",
                !open && long && (item.kind === "tool" ? "line-clamp-2" : "line-clamp-3"),
              )}
            >
              {item.detail}
            </span>
          )}
        </button>
      </div>
      <time className="shrink-0 font-mono text-[10.5px] text-faint opacity-0 group-hover:opacity-100">{formatClock(item.at)}</time>
    </li>
  );
}

export function Timeline({ view }: { view: RunView }) {
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    if (pinned.current) end.current?.scrollIntoView({ block: "end" });
  }, [view.timeline.length]);
  if (view.timeline.length === 0) return <Empty>Waiting for the first event…</Empty>;
  return (
    <div
      ref={box}
      className="h-full overflow-y-auto px-3 py-2"
      onScroll={() => {
        const el = box.current;
        if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      }}
    >
      <ol>
        {view.timeline.map((item, index) => (
          <Row key={`${item.id}-${index}`} item={item} />
        ))}
      </ol>
      <div ref={end} />
    </div>
  );
}
