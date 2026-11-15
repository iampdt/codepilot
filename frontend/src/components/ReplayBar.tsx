import type { Playback } from "../lib/useRun";
import { Button, cx } from "./ui";

/** Transport for a recorded run: play, pause, speed, and a scrubber over its events. */
export function ReplayBar({ playback }: { playback: Playback }) {
  const done = playback.position >= playback.total;
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-line bg-[#0d1220] px-4 py-2 text-xs">
      <span className="rounded border border-accent/40 px-1.5 py-0.5 font-semibold uppercase tracking-wider text-accent">Recording</span>
      <span className="hidden text-muted sm:inline">A real run of the system, replayed from its event log. Long waits are shortened.</span>
      <div className="ml-auto flex items-center gap-2">
        <Button variant="secondary" className="!px-2.5 !py-1 !text-xs" onClick={playback.toggle}>
          {done ? "↺ Replay" : playback.playing ? "❚❚ Pause" : "▶ Play"}
        </Button>
        <input
          type="range"
          min={0}
          max={playback.total}
          value={playback.position}
          onChange={(e) => playback.seek(Number(e.target.value))}
          className="w-32 accent-[#5eead4] sm:w-56"
          aria-label="Position in the recording"
        />
        <span className="w-20 font-mono text-faint">
          {playback.position}/{playback.total}
        </span>
        {[1, 4, 16].map((speed) => (
          <button
            key={speed}
            type="button"
            onClick={() => playback.setSpeed(speed)}
            className={cx("rounded px-1.5 py-0.5 font-mono", playback.speed === speed ? "bg-accent text-accent-ink" : "text-muted hover:text-text")}
            aria-pressed={playback.speed === speed}
          >
            {speed}×
          </button>
        ))}
        <Button variant="ghost" className="!px-1.5 !py-1 !text-xs" onClick={() => playback.seek(playback.total)}>
          Skip to end
        </Button>
      </div>
    </div>
  );
}
