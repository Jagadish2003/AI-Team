import React from 'react';
import { RunStep } from '../../types/discoveryRun';
import { Check, Play, AlertTriangle, Clock } from 'lucide-react';

function icon(status: string) {
  if (status === 'DONE')    return <Check         size={14} strokeWidth={2.5} />;
  if (status === 'RUNNING') return <Play          size={14} strokeWidth={2.5} />;
  if (status === 'FAILED')  return <AlertTriangle size={14} strokeWidth={2.5} />;
  return                           <Clock         size={14} strokeWidth={2} />;
}

export default function PipelineStepper({ steps }: { steps: RunStep[] }) {
  return (
    <div className="flex flex-col rounded-xl border border-border bg-panel p-5" style={{ minHeight: '100%' }}>
      <div className="mb-4 text-xl font-semibold text-text">Pipeline Progress</div>
      <div className="flex flex-1 flex-col justify-between gap-1">
        {steps.map((s) => (
          <div
            key={s.id}
            className="flex items-center justify-between gap-3 rounded-lg px-3 py-3 transition-colors hover:bg-white/5"
          >
            <div className="flex items-center gap-3">
              <div
                className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full border text-sm ${
                  s.status === 'DONE'
                    ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                    : s.status === 'RUNNING'
                      ? 'border-accent/60 bg-accent/10 text-text'
                      : s.status === 'FAILED'
                        ? 'border-danger/60 bg-danger/10 text-danger'
                        : 'border-border bg-bg/20 text-muted'
                }`}
              >
                {icon(s.status)}
              </div>

              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-text">{s.label}</div>
              </div>
            </div>

            <div
              className={`shrink-0 rounded-md border px-2.5 py-1 text-xs font-medium ${
                s.status === 'DONE'
                  ? 'border-emerald-500/30 text-emerald-300'
                  : s.status === 'RUNNING'
                    ? 'border-accent/40 text-text'
                    : s.status === 'FAILED'
                      ? 'border-danger/40 text-danger'
                      : 'border-border text-muted'
              }`}
            >
              {s.status}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}