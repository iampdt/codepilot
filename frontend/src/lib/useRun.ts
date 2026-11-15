// One hook for both modes. Live: fetch the task and follow its event stream. Replay: load an
// exported run and feed its events through the same reducer on a timer.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { applyEvent, emptyView, type RunView } from "./reduce";
import { api, DEMO, followEvents, replay, replayDelays } from "./source";
import { TERMINAL, type Change, type FileEntry, type RunBundle, type RunEvent, type Task } from "./types";

export interface Playback {
  playing: boolean;
  speed: number;
  position: number;
  total: number;
  toggle: () => void;
  setSpeed: (speed: number) => void;
  seek: (position: number) => void;
}

export interface RunFiles {
  list: () => Promise<FileEntry[]>;
  read: (path: string, version: "current" | "base") => Promise<{ content: string; missing?: boolean }>;
  changes: () => Promise<{ changes: Change[]; diff: string }>;
}

export interface Run {
  task: Task | null;
  view: RunView;
  error: string | null;
  connected: boolean;
  demo: boolean;
  files: RunFiles;
  playback: Playback | null;
  refresh: () => Promise<void>;
  /** Bumps whenever files in the workspace may have changed. */
  filesVersion: number;
}

function viewFrom(events: RunEvent[], count: number): RunView {
  const view = emptyView();
  for (let i = 0; i < count; i++) applyEvent(view, events[i]!);
  return view;
}

export function useRun(taskId: string): Run {
  const [task, setTask] = useState<Task | null>(null);
  const [view, setView] = useState<RunView>(emptyView);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [filesVersion, setFilesVersion] = useState(0);
  const viewRef = useRef<RunView>(emptyView());

  // --- replay state ---
  const [bundle, setBundle] = useState<RunBundle | null>(null);
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(1);
  const delays = useMemo(() => (bundle ? replayDelays(bundle.events) : []), [bundle]);

  const refresh = useCallback(async () => {
    if (DEMO) return;
    try {
      setTask(await api.task(taskId));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }, [taskId]);

  // Live mode.
  useEffect(() => {
    if (DEMO) return;
    viewRef.current = emptyView();
    setView(emptyView());
    setError(null);
    void refresh();
    let pending = false;
    let refetch: ReturnType<typeof setTimeout> | undefined;
    const flush = () => {
      pending = false;
      setView({ ...viewRef.current });
    };
    const stop = followEvents(
      taskId,
      0,
      (event) => {
        applyEvent(viewRef.current, event);
        // Output arrives in bursts; one render per frame is enough.
        if (!pending) {
          pending = true;
          requestAnimationFrame(flush);
        }
        if (["state_changed", "plan_ready", "review_ready", "result_ready"].includes(event.type)) {
          clearTimeout(refetch);
          refetch = setTimeout(() => void refresh(), 150);
        }
        if (["file_changed", "checkpoint_restored", "command_finished", "result_ready"].includes(event.type)) {
          setFilesVersion((v) => v + 1);
        }
      },
      () => {
        setConnected(false);
        void refresh();
      },
      setConnected,
    );
    return () => {
      stop();
      clearTimeout(refetch);
    };
  }, [taskId, refresh]);

  // Replay mode: load the bundle.
  useEffect(() => {
    if (!DEMO) return;
    setBundle(null);
    setPosition(0);
    setPlaying(true);
    setError(null);
    replay
      .bundle(taskId)
      .then(setBundle)
      .catch((cause: Error) => setError(cause.message));
  }, [taskId]);

  // Replay mode: advance one event at a time.
  useEffect(() => {
    if (!DEMO || !bundle || !playing || position >= bundle.events.length) return;
    const timer = setTimeout(() => setPosition((p) => p + 1), (delays[position] ?? 100) / speed);
    return () => clearTimeout(timer);
  }, [bundle, playing, position, speed, delays]);

  const replayView = useMemo(
    () => (DEMO && bundle ? viewFrom(bundle.events, position) : null),
    [bundle, position],
  );
  const finished = DEMO && bundle ? position >= bundle.events.length : false;

  // In a replay the task record is shown as it was at the playhead, not as it ended.
  const replayTask = useMemo<Task | null>(() => {
    if (!DEMO || !bundle || !replayView) return null;
    if (finished) return bundle.task;
    return {
      ...bundle.task,
      state: replayView.state,
      error: replayView.error,
      plan: replayView.plan,
      usage: replayView.usage,
      changes: replayView.changes,
      isolation: replayView.isolation,
      has_result: false,
      finished_at: null,
      reports: {
        ...(replayView.review ? { review: replayView.review } : {}),
      },
    };
  }, [bundle, replayView, finished]);

  const touchedCount = replayView ? Object.keys(replayView.touched).length : 0;
  useEffect(() => {
    if (DEMO) setFilesVersion((v) => v + 1);
  }, [touchedCount, finished]);

  const files = useMemo<RunFiles>(() => {
    if (!DEMO) {
      return {
        list: async () => (await api.files(taskId)).files,
        read: (path, version) => api.file(taskId, path, version),
        changes: () => api.diff(taskId),
      };
    }
    const visible = (path: string) =>
      finished || !!replayView?.touched[path] || !(bundle?.task.changes ?? []).some((c) => c.path === path);
    return {
      list: async () =>
        Object.entries(bundle?.files ?? {})
          .filter(([path]) => visible(path))
          .map(([path, content]) => ({ path, size: content.length }))
          .sort((a, b) => a.path.localeCompare(b.path)),
      read: async (path, version) => {
        const source = version === "base" ? bundle?.base_files : bundle?.files;
        const content = source?.[path];
        return content === undefined ? { content: "", missing: true } : { content };
      },
      changes: async () => ({
        changes: (bundle?.task.changes ?? []).filter((c) => finished || !!replayView?.touched[c.path]),
        diff: finished ? (bundle?.diff ?? "") : "",
      }),
    };
  }, [taskId, bundle, replayView, finished]);

  const playback = useMemo<Playback | null>(() => {
    if (!DEMO || !bundle) return null;
    return {
      playing: playing && !finished,
      speed,
      position,
      total: bundle.events.length,
      toggle: () => {
        if (finished) {
          setPosition(0);
          setPlaying(true);
        } else setPlaying((p) => !p);
      },
      setSpeed,
      seek: (next) => setPosition(Math.max(0, Math.min(next, bundle.events.length))),
    };
  }, [bundle, playing, speed, position, finished]);

  if (DEMO) {
    return {
      task: replayTask,
      view: replayView ?? emptyView(),
      error,
      connected: false,
      demo: true,
      files,
      playback,
      refresh,
      filesVersion,
    };
  }
  const live = task && !TERMINAL.includes(task.state);
  return { task, view, error, connected: connected && !!live, demo: false, files, playback: null, refresh, filesVersion };
}
