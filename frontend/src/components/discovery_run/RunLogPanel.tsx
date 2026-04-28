import React, { useEffect, useRef } from 'react';
import { RunEvent } from '../../types/discoveryRun';

export default function RunLogPanel({
  events,
  autoScroll,
  onToggleAutoScroll
}: {
  events: RunEvent[];
  autoScroll: boolean;
  onToggleAutoScroll: (v: boolean) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!autoScroll) return;
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [events, autoScroll]);

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="flex items-center justify-between">
        <div className="text-xl font-semibold text-text">Live Run Log</div>
        <label className="flex items-center gap-2 text-xs text-muted">
        <input
          type="checkbox"
          className="h-4 w-4 cursor-pointer appearance-auto accent-[#0D55D7] bg-[#0D55D7] border-[#0D55D7]"
          checked={autoScroll}
          onChange={(e) => onToggleAutoScroll(e.target.checked)}
        />
          Auto-scroll
        </label>
      </div>

      <div ref={ref} className="mt-3 h-[78vh] overflow-y-auto rounded-lg border border-border bg-bg/20 p-2">
        {events.map((e, idx) => (
          <div key={idx} className="border-b border-border/40 px-2 py-2 text-sm">
            <div className="flex items-center gap-2 text-xs text-muted">
              <span className="rounded bg-bg/40 px-1.5 py-0.5">{e.ts}</span>
              <span
                className={`rounded px-1.5 py-0.5 ${
                  e.level === 'ERROR'
                    ? 'bg-danger/15 text-danger'
                    : e.level === 'WARNING'
                      ? 'bg-amber-500/15 text-amber-200'
                      : 'bg-slate-500/10 text-muted'
                }`}
              >
                {e.level}
              </span>
            </div>
            <div className="mt-1 text-text">{e.message}</div>
          </div>
        ))}
      </div>
    </div>
  );
}