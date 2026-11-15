import { DiffEditor, Editor } from "@monaco-editor/react";
import { useEffect, useState } from "react";
import type { RunFiles } from "../lib/useRun";
import type { Change, FileEntry } from "../lib/types";
import { cx, Empty } from "./ui";

const LANGUAGES: Record<string, string> = {
  py: "python", ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
  json: "json", md: "markdown", yml: "yaml", yaml: "yaml", toml: "ini", ini: "ini", cfg: "ini",
  html: "html", css: "css", sh: "shell", sql: "sql", txt: "plaintext", dockerfile: "dockerfile",
};

function languageOf(path: string): string {
  const name = path.split("/").pop() ?? "";
  if (name.toLowerCase() === "dockerfile") return "dockerfile";
  return LANGUAGES[name.split(".").pop()?.toLowerCase() ?? ""] ?? "plaintext";
}

const EDITOR_OPTIONS = {
  readOnly: true,
  minimap: { enabled: false },
  fontSize: 12.5,
  fontFamily: '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace',
  scrollBeyondLastLine: false,
  renderLineHighlight: "none" as const,
  padding: { top: 10 },
  automaticLayout: true,
};

function FileList({
  items,
  selected,
  onSelect,
}: {
  items: { path: string; badge?: string; tone?: string; meta?: string }[];
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  return (
    <ul className="h-full overflow-y-auto py-1.5 text-[12.5px]">
      {items.map((item) => (
        <li key={item.path}>
          <button
            type="button"
            onClick={() => onSelect(item.path)}
            className={cx(
              "flex w-full items-center gap-2 px-3 py-1 text-left font-mono hover:bg-raised",
              selected === item.path && "bg-raised text-accent",
            )}
            title={item.path}
          >
            {item.badge && <span className={cx("w-3 shrink-0 text-center text-[11px] font-bold", item.tone)}>{item.badge}</span>}
            <span className="min-w-0 flex-1 truncate" dir="rtl">
              <bdi>{item.path}</bdi>
            </span>
            {item.meta && <span className="shrink-0 text-[10.5px] text-faint">{item.meta}</span>}
          </button>
        </li>
      ))}
    </ul>
  );
}

function Split({ side, children }: { side: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="grid h-full grid-cols-[minmax(170px,240px)_minmax(0,1fr)]">
      <div className="min-h-0 border-r border-line">{side}</div>
      <div className="min-h-0 bg-[#1e1e1e]">{children}</div>
    </div>
  );
}

/** Every file in the workspace, read-only, in Monaco. */
export function FilesPanel({ files, version, touched }: { files: RunFiles; version: number; touched: Record<string, string> }) {
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<string>("");

  useEffect(() => {
    let live = true;
    files
      .list()
      .then((list) => {
        if (!live) return;
        setEntries(list);
        setSelected((current) => (current && list.some((f) => f.path === current) ? current : (list[0]?.path ?? null)));
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [files, version]);

  useEffect(() => {
    if (!selected) return;
    let live = true;
    files
      .read(selected, "current")
      .then((file) => live && setContent(file.content))
      .catch(() => live && setContent("// this file can't be shown (binary, or the sandbox is busy)"));
    return () => {
      live = false;
    };
  }, [files, selected, version]);

  if (entries.length === 0) return <Empty>The workspace is empty so far.</Empty>;
  return (
    <Split
      side={
        <FileList
          items={entries.map((f) => ({
            path: f.path,
            badge: touched[f.path] ? "●" : undefined,
            tone: "text-accent",
            meta: f.size > 1024 ? `${(f.size / 1024).toFixed(1)}k` : `${f.size}b`,
          }))}
          selected={selected}
          onSelect={setSelected}
        />
      }
    >
      {selected && <Editor theme="vs-dark" path={selected} language={languageOf(selected)} value={content} options={EDITOR_OPTIONS} />}
    </Split>
  );
}

const STATUS: Record<Change["status"], { badge: string; tone: string }> = {
  added: { badge: "A", tone: "text-ok" },
  modified: { badge: "M", tone: "text-warn" },
  deleted: { badge: "D", tone: "text-bad" },
};

/** What the task changed, file by file, as a side-by-side diff against where it started. */
export function ChangesPanel({ files, version }: { files: RunFiles; version: number }) {
  const [changes, setChanges] = useState<Change[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [pair, setPair] = useState<{ base: string; current: string } | null>(null);

  useEffect(() => {
    let live = true;
    files
      .changes()
      .then((result) => {
        if (!live) return;
        setChanges(result.changes);
        setSelected((current) => (current && result.changes.some((c) => c.path === current) ? current : (result.changes[0]?.path ?? null)));
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [files, version]);

  useEffect(() => {
    if (!selected) return;
    let live = true;
    Promise.all([files.read(selected, "base"), files.read(selected, "current")])
      .then(([base, current]) => live && setPair({ base: base.content, current: current.content }))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [files, selected, version]);

  if (changes.length === 0) return <Empty>No file changes yet.</Empty>;
  const added = changes.reduce((n, c) => n + c.added, 0);
  const removed = changes.reduce((n, c) => n + c.removed, 0);
  return (
    <Split
      side={
        <div className="flex h-full flex-col">
          <p className="border-b border-line px-3 py-2 text-xs text-muted">
            {changes.length} file{changes.length === 1 ? "" : "s"} <span className="text-ok">+{added}</span> <span className="text-bad">−{removed}</span>
          </p>
          <div className="min-h-0 flex-1">
            <FileList
              items={changes.map((c) => ({ path: c.path, badge: STATUS[c.status].badge, tone: STATUS[c.status].tone, meta: `+${c.added} −${c.removed}` }))}
              selected={selected}
              onSelect={setSelected}
            />
          </div>
        </div>
      }
    >
      {selected && pair && (
        <DiffEditor
          theme="vs-dark"
          language={languageOf(selected)}
          original={pair.base}
          modified={pair.current}
          // A new or deleted file has nothing on one side: show it as one column.
          options={{
            ...EDITOR_OPTIONS,
            renderSideBySide: changes.find((c) => c.path === selected)?.status === "modified",
            originalEditable: false,
          }}
        />
      )}
    </Split>
  );
}
