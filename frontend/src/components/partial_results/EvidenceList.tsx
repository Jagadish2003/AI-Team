import React, { useState, useRef, useEffect } from 'react';
import { ChevronLeft, ChevronRight, ChevronDown, Search } from 'lucide-react';
import { EvidenceReview } from '../../types/partialResults';

function decisionClass(decision: string) {
  const cls =
    decision === 'APPROVED' ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300'
    : decision === 'REJECTED' ? 'border-red-500/50 bg-red-500/15 text-red-300'
    : 'border-border bg-bg/30 text-muted';
  return `rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide whitespace-nowrap ${cls}`;
}

function confidenceClass(value: string) {
  const cls =
    value === 'HIGH' ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300'
    : value === 'MEDIUM' ? 'border-amber-500/50 bg-amber-500/15 text-amber-300'
    : 'border-red-500/50 bg-red-500/15 text-red-300';
  return `rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide whitespace-nowrap ${cls}`;
}

function SourceDropdown({ sources, value, onChange }: {
  sources: string[]; value: string; onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);
  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="cursor-pointer whitespace-nowrap rounded-lg border border-border bg-panel px-3 py-2 text-sm text-text focus:outline-none focus:ring-1 focus:ring-accent"
      >
        <span className="flex items-center gap-2">
          {value}
          <ChevronDown size={14} className={`text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
        </span>
      </button>
      {open && (
        <div className="absolute right-0 z-50 mt-1 min-w-full overflow-hidden rounded-lg border border-border bg-panel shadow-lg">
          {sources.map((s) => (
            <div
              key={s}
              onClick={() => { onChange(s); setOpen(false); }}
              className={`cursor-pointer px-4 py-2 text-sm transition-colors ${
                s === value ? 'bg-[#00B4B4] font-medium text-[#0d1117]' : 'text-text hover:bg-[#00B4B4]/15 hover:text-[#00B4B4]'
              }`}
            >
              {s}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function EvidenceList({
  evidence, selectedId, onSelect, sources, sourceFilter, onSourceFilter,
  query, onQuery, saveDraftEnabled, onSaveDraftEnabled,
  positionLabel, canPrev, canNext, onPrev, onNext
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
  canPrev: boolean;
  canNext: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="text-xl font-semibold text-text pb-3">{evidence.length} Evidence Snippets</div>
        <label className="cursor-pointer text-xs text-muted">
          <span className="flex items-center gap-2">
            <input
              type="checkbox"
              className="accent-[#00B4B4]"
              checked={saveDraftEnabled}
              onChange={(e) => onSaveDraftEnabled(e.target.checked)}
            />
            Save Draft
          </span>
        </label>
      </div>

      {/* Search + filter */}
      <div className="mt-3 flex gap-2">
        <div className="relative flex-1">
          <input
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder="Search evidence…"
            className="w-full rounded-md border border-border bg-bg/30 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted transition-colors hover:bg-bg/50 hover:border-[#00B4B4]/50 focus:outline-none focus:border-[#00B4B4] focus:ring-2 focus:ring-[#00B4B4]/50 appearance-none"
          />
          <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
        </div>
        <SourceDropdown sources={sources} value={sourceFilter} onChange={onSourceFilter} />
      </div>
      <div className="mt-3 h-[520px] overflow-y-auto rounded-lg border border-border bg-bg/20">
        {evidence.map((ev) => {
          const selected = ev.id === selectedId;
          return (
            <div
              key={ev.id}
              onClick={() => onSelect(ev.id)}
              className={`cursor-pointer border-b border-border/50 p-3 ${
                selected ? 'border-l-2 border-l-[#00B4B4] bg-[#00B4B4]/10' : 'hover:bg-panel2'
              }`}
            >
              <div className="flex items-center justify-between text-xs text-muted">
                <span>{ev.tsLabel}</span>
                <span className={confidenceClass(ev.confidence)}>{ev.confidence}</span>
              </div>
              <div className={`mt-1 text-sm font-semibold ${selected ? 'text-accent' : 'text-text'}`}>
                {ev.title}
              </div>
              <div className="mt-1 line-clamp-2 text-sm text-muted">{ev.snippet}</div>
              <div className="mt-2 flex items-center gap-2 flex-wrap">
                <span className="rounded-full border border-border bg-bg/30 px-2 py-0.5 text-[10px] text-text whitespace-nowrap">{ev.source}</span>
                <span className="rounded-full border border-border bg-bg/30 px-2 py-0.5 text-[10px] text-text whitespace-nowrap">{ev.evidenceType}</span>
                <span className={decisionClass(ev.decision)}>{ev.decision}</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Pagination */}
      <div className="mt-3 flex items-center justify-between text-sm text-text">
        <button
          type="button"
          disabled={!canPrev}
          onClick={onPrev}
          className="flex items-center gap-1 rounded border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ChevronLeft className="h-4 w-4" /> Prev
        </button>
        <span>{positionLabel}</span>
        <button
          type="button"
          disabled={!canNext}
          onClick={onNext}
          className="flex items-center gap-1 rounded border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next <ChevronRight className="h-4 w-4" />
        </button>
      </div>

    </div>
  );
}