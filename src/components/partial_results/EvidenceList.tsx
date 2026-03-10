import React from 'react';
import Button from '../common/Button';
import { EvidenceReview } from '../../types/partialResults';

function decisionClass(decision: string) {
  if (decision === 'APPROVED') return 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30';
  if (decision === 'REJECTED') return 'bg-red-500/15 text-red-300 border-red-500/30';
  return 'bg-slate-500/10 text-muted border-border';
}

export default function EvidenceList({
  evidence,
  selectedId,
  onSelect,
  sources,
  sourceFilter,
  onSourceFilter,
  query,
  onQuery,
  saveDraftEnabled,
  onSaveDraftEnabled,
  positionLabel,
  onPrev,
  onNext,
  onPaginationToast
}: {
  evidence: EvidenceReview[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  sources: string[];
  sourceFilter: string;
  onSourceFilter: (v: string) => void;
  query: string;
  onQuery: (v: string) => void;
  saveDraftEnabled: boolean;
  onSaveDraftEnabled: (v: boolean) => void;
  positionLabel: string;
  onPrev: () => void;
  onNext: () => void;
  onPaginationToast: () => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-text">{evidence.length} Evidence Snippets</div>
        <label className="flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={saveDraftEnabled} onChange={(e) => onSaveDraftEnabled(e.target.checked)} />
          Save Draft
        </label>
      </div>

      <div className="mt-3 flex gap-2">
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Search evidence…"
          className="flex-1 rounded-md border border-border bg-bg/30 px-3 py-2 text-sm text-text placeholder:text-muted"
        />
        <select
          value={sourceFilter}
          onChange={(e) => onSourceFilter(e.target.value)}
          className="rounded-md border border-border bg-bg/30 px-3 py-2 text-sm text-text"
        >
          {sources.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      <div className="mt-3 h-[520px] overflow-auto rounded-lg border border-border bg-bg/20">
        {evidence.map(ev => {
          const selected = ev.id === selectedId;
          return (
            <div
              key={ev.id}
              onClick={() => onSelect(ev.id)}
              className={`cursor-pointer border-b border-border/50 p-3 ${
                selected ? 'bg-panel2' : 'hover:bg-panel2'
              }`}
            >
              <div className="flex items-center justify-between text-xs text-muted">
                <span>{ev.tsLabel}</span>
                <span className="rounded bg-bg/40 px-2 py-0.5">{ev.confidence}</span>
              </div>
              <div className="mt-1 text-sm font-semibold text-text">{ev.title}</div>
              <div className="mt-1 text-sm text-muted line-clamp-2">{ev.snippet}</div>

              <div className="mt-2 flex items-center gap-2 text-xs text-muted">
                <span className="rounded border border-border bg-bg/30 px-2 py-0.5">{ev.source}</span>
                <span className="rounded border border-border bg-bg/30 px-2 py-0.5">{ev.evidenceType}</span>
                <span className={`rounded border px-2 py-0.5 ${decisionClass(ev.decision)}`}>{ev.decision}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-3 flex items-center justify-between text-xs text-muted">
        <Button variant="secondary" onClick={() => { onPrev(); }} disabled={evidence.length === 0}>Prev</Button>
        <span>{positionLabel}</span>
        <Button variant="secondary" onClick={() => { onNext(); }} disabled={evidence.length === 0}>Next</Button>
      </div>

      <div className="mt-2 text-xs text-muted">
        Pagination is item-based for Sprint 1 (list paging in Sprint 2).{' '}
        <button className="underline" onClick={onPaginationToast}>Learn more</button>
      </div>
    </div>
  );
}
