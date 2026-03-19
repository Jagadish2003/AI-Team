import React, { useState, useRef, useEffect } from 'react';
import { ChevronLeft, ChevronRight, ChevronDown, Search } from 'lucide-react';
import Button from '../common/Button';
import { EvidenceReview } from '../../types/partialResults';

function decisionClass(decision: string) {
  if (decision === 'APPROVED') return 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30';
  if (decision === 'REJECTED') return 'bg-red-500/15 text-red-300 border-red-500/30';
  return 'bg-slate-500/10 text-muted border-border';
}

function SourceDropdown({
  sources,
  value,
  onChange
}: {
  sources: string[];
  value: string;
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
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
              onClick={() => {
                onChange(s);
                setOpen(false);
              }}
              className={`cursor-pointer px-4 py-2 text-sm transition-colors ${
                s === value
                  ? 'bg-[#00B4B4] font-medium text-[#0d1117]'
                  : 'text-text hover:bg-[#00B4B4]/15 hover:text-[#00B4B4]'
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

      <div className="mt-3 flex gap-2">
        {/* Search input with icon and hover/focus teal border */}
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

      <div className="mt-3 h-[520px] overflow-auto rounded-lg border border-border bg-bg/20">
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
                <span className="rounded bg-bg/40 px-2 py-0.5">{ev.confidence}</span>
              </div>
              <div className="mt-1 text-sm font-semibold text-text">{ev.title}</div>
              <div className="mt-1 line-clamp-2 text-sm text-muted">{ev.snippet}</div>

              <div className="mt-2 flex items-center gap-2 text-xs text-muted">
                <span className="rounded border border-border bg-bg/30 px-2 py-0.5">{ev.source}</span>
                <span className="rounded border border-border bg-bg/30 px-2 py-0.5">{ev.evidenceType}</span>
                <span className={`rounded border px-2 py-0.5 ${decisionClass(ev.decision)}`}>{ev.decision}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-3 flex items-center justify-between text-xs text-muted ">
        <Button variant="secondary" onClick={onPrev} disabled={evidence.length === 0} className='border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40'>
          <ChevronLeft className="h-4 w-4 " />
          Prev
        </Button>
        <span>{positionLabel}</span>
        <Button variant="secondary" onClick={onNext} disabled={evidence.length === 0} className='border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40'>
          Next
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>

      <div className="mt-2 text-xs text-muted">
        <button className="underline" onClick={onPaginationToast}>
          Learn more
        </button>
      </div>
    </div>
  );
}