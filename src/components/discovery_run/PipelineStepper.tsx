import React from 'react';
import { RunStep } from '../../types/discoveryRun';

function icon(status: string) {
  if (status === 'DONE') return '✓';
  if (status === 'RUNNING') return '▶';
  if (status === 'FAILED') return '!';
  return '○';
}

export default function PipelineStepper({ steps }: { steps: RunStep[] }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-semibold text-text">Pipeline Progress</div>
      <div className="mt-3 space-y-3">
        {steps.map(s => (
          <div key={s.id} className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className={`flex h-8 w-8 items-center justify-center rounded-full border ${
                s.status === 'DONE' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
                s.status === 'RUNNING' ? 'border-accent/60 bg-accent/10 text-text' :
                s.status === 'FAILED' ? 'border-danger/60 bg-danger/10 text-danger' :
                'border-border bg-bg/20 text-muted'
              }`}>
                {icon(s.status)}
              </div>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-text">{s.label}</div>
                <div className="text-xs text-muted">{s.status}</div>
              </div>
            </div>
            <div className={`rounded-md border px-2 py-1 text-xs ${
              s.status === 'DONE' ? 'border-emerald-500/30 text-emerald-200' :
              s.status === 'RUNNING' ? 'border-accent/40 text-text' :
              s.status === 'FAILED' ? 'border-danger/40 text-danger' :
              'border-border text-muted'
            }`}>
              {s.status}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
