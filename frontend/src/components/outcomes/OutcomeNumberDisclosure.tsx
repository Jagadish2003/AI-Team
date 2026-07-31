import React from 'react';
import { FileText } from 'lucide-react';
import type { OutcomeNumberRef } from '../../types/outcome';

function shortRun(runId?: string | null): string {
  if (!runId) return 'Unavailable';
  return runId.length > 12 ? runId.slice(0, 12) : runId;
}

export function formatOutcomeNumber(
  value: number | null | undefined,
  unit?: string | null,
): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return 'Unavailable';
  }
  const formatted = new Intl.NumberFormat('en-US', {
    maximumFractionDigits: unit === 'percent' ? 1 : 2,
  }).format(Number(value));
  return unit === 'percent' ? `${formatted}%` : formatted;
}

export function OutcomeNumberDisclosure({ refItem }: { refItem: OutcomeNumberRef }) {
  const evidence = refItem.evidence ?? {};
  const runPairs = evidence.runPairs ?? [];
  const runIds = evidence.runIds ?? [];
  const lifecycles = evidence.lifecycles ?? [];
  return (
    <details className="rounded-md border border-border bg-bg/40">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 text-sm marker:hidden">
        <span className="flex min-w-0 items-center gap-2 text-muted">
          <FileText className="h-4 w-4 shrink-0 text-accent" aria-hidden />
          <span className="truncate">{refItem.label}</span>
        </span>
        <span className="shrink-0 font-semibold text-text">
          {formatOutcomeNumber(refItem.value, refItem.unit)}
        </span>
      </summary>
      <div className="border-t border-border px-3 py-2 text-xs text-muted">
        {evidence.baselineRunId || evidence.currentRunId ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <div>
              <div className="font-semibold uppercase tracking-wide text-muted/80">
                Baseline Run
              </div>
              <div className="mt-0.5 font-mono text-text">
                {shortRun(evidence.baselineRunId)}
              </div>
            </div>
            <div>
              <div className="font-semibold uppercase tracking-wide text-muted/80">
                Current Run
              </div>
              <div className="mt-0.5 font-mono text-text">
                {shortRun(evidence.currentRunId)}
              </div>
            </div>
          </div>
        ) : null}
        {runIds.length > 0 ? (
          <div className="mt-2">
            <div className="font-semibold uppercase tracking-wide text-muted/80">
              Run Ids
            </div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {runIds.slice(0, 8).map((runId) => (
                <span
                  key={runId}
                  className="rounded border border-border bg-panel px-1.5 py-0.5 font-mono text-[11px] text-text"
                >
                  {shortRun(runId)}
                </span>
              ))}
            </div>
          </div>
        ) : null}
        {runPairs.length > 0 ? (
          <div className="mt-2 space-y-1">
            <div className="font-semibold uppercase tracking-wide text-muted/80">
              Measurement Pairs
            </div>
            {runPairs.slice(0, 5).map((pair, index) => (
              <div key={`${pair.opportunityIdentity}-${index}`} className="font-mono text-[11px] text-text">
                {shortRun(pair.baselineRunId)} {'->'} {shortRun(pair.currentRunId)}
              </div>
            ))}
          </div>
        ) : null}
        {lifecycles.length > 0 ? (
          <div className="mt-2 space-y-1">
            <div className="font-semibold uppercase tracking-wide text-muted/80">
              Lifecycle Evidence
            </div>
            {lifecycles.slice(0, 5).map((lifecycle, index) => (
              <div
                key={`${lifecycle.opportunityIdentity}-${index}`}
                className="font-mono text-[11px] text-text"
              >
                {lifecycle.opportunityIdentity ?? 'unknown'} - {lifecycle.state ?? 'unknown'}
              </div>
            ))}
          </div>
        ) : null}
        {evidence.confounderSummary ? (
          <div className="mt-2">
            Caveats: {evidence.confounderSummary.count}
            {evidence.confounderSummary.materialCount
              ? ` (${evidence.confounderSummary.materialCount} material)`
              : ''}
          </div>
        ) : null}
      </div>
    </details>
  );
}
