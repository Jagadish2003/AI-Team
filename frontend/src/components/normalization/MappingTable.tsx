import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNormalizationContext } from '../../context/NormalizationContext';
import { ChevronDown, ChevronLeft, ChevronRight, Search } from 'lucide-react';

const PAGE_SIZE = 8;
 
type Tab = 'MAPPED' | 'UNMAPPED' | 'AMBIGUOUS';
type SortMode = 'Confidence High→Low' | 'Source A→Z';
 
const sortOptions: SortMode[] = ['Confidence High→Low', 'Source A→Z'];
 
function pill(value: string) {
  const cls =
    value === 'HIGH'
      ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300'
      : value === 'MEDIUM'
      ? 'border-amber-500/50 bg-amber-500/15 text-amber-300'
      : 'border-red-500/50 bg-red-500/15 text-red-300';
  return <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide whitespace-nowrap ${cls}`}>{value}</span>;
}
 
export default function MappingTable() {
  const {
    filteredRows, selectedRowId, setSelectedRowId,
    search, setSearch, sortMode, setSortMode,
    activeTab, setActiveTab, counts,
  } = useNormalizationContext();
 
  const [currentPage, setCurrentPage] = useState(1);
  const [sortOpen, setSortOpen] = useState(false);
  const [hoveredRowId, setHoveredRowId] = useState<string | null>(null);
  const sortRef = useRef<HTMLDivElement>(null);
 
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (sortRef.current && !sortRef.current.contains(e.target as Node)) setSortOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);
 
  useEffect(() => { setCurrentPage(1); }, [activeTab, search, sortMode, filteredRows.length]);
 
  const totalPages = Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE));
 
  const pageRows = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE;
    return filteredRows.slice(start, start + PAGE_SIZE);
  }, [filteredRows, currentPage]);
 
  useEffect(() => {
    if (pageRows.length === 0) return;
    if (!pageRows.some(row => row.id === selectedRowId)) setSelectedRowId(pageRows[0].id);
  }, [pageRows, selectedRowId, setSelectedRowId]);
 
  const canPrev = currentPage > 1;
  const canNext = currentPage < totalPages;
 
  const tabButton = (id: Tab, label: string) => (
    <button type="button" onClick={() => setActiveTab(id)}
      className={`w-full rounded-md border px-3 py-2.5 text-sm font-semibold transition ${
        activeTab === id
          ? 'border-accent/60 bg-panel2 text-text'
          : 'border-border bg-bg/20 text-muted hover:bg-panel2 hover:text-text'
      }`}>
      <span>{label}</span>
      <span className="ml-1">({counts[id]})</span>
    </button>
  );
 
  return (
    <div className="flex h-full flex-col rounded-xl border border-border bg-panel p-4">
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-3 gap-3">
          {tabButton('MAPPED', 'Mapped')}
          {tabButton('UNMAPPED', 'Unmapped')}
          {tabButton('AMBIGUOUS', 'Ambiguous')}
        </div>

        <div className="grid grid-cols-[minmax(0,1.8fr)_minmax(220px,1fr)] gap-3">
          <div className="relative">
            <input
              value={search} onChange={e => setSearch(e.target.value)}
              placeholder="Search fields..."
              className="w-full rounded-md border border-border bg-bg/30 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted hover:bg-bg/50 hover:border-accent/50 transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50 appearance-none"
            />
            <Search className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted pointer-events-none" />
          </div>
 
          <div className="relative" ref={sortRef}>
            <button type="button" onClick={() => setSortOpen(v => !v)}
              className="flex w-full items-center gap-2 rounded-md border border-border bg-bg/30 px-3 py-2 text-sm text-text hover:bg-bg/50 hover:border-accent/50 transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50">
              <span className="flex-1 truncate text-left">Sort: {sortMode}</span>
              <ChevronDown size={14} className={`shrink-0 text-muted transition-transform ${sortOpen ? 'rotate-180' : ''}`} />
            </button>
            {sortOpen && (
              <div className="absolute right-0 z-50 mt-1 min-w-full overflow-hidden rounded-lg border border-border bg-panel shadow-lg">
                {sortOptions.map(option => (
                  <div key={option}
                    onClick={() => { setSortMode(option); setSortOpen(false); }}
                    className={`cursor-pointer px-4 py-2 text-sm transition-colors ${
                      option === sortMode
                        ? 'bg-[#00B4B4] font-medium text-[#0d1117]'
                        : 'text-text hover:bg-[#00B4B4]/15 hover:text-[#00B4B4]'
                    }`}>
                    {option}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
 
    <div className="mt-3 grid grid-cols-[2fr_1fr_0.3fr_1.5fr_0.8fr] bg-bg/20 px-4 py-2 text-xs font-semibold text-text rounded-t-lg border border-border">
      <div>Source Field</div>
      <div>Source Type</div>
      <div className="text-center">→</div>
      <div>Common Field</div>
      <div>Confidence</div>
    </div>

<div className="flex flex-col overflow-y-auto flex-1 rounded-b-lg border border-t-0 border-border bg-bg/20">
  {pageRows.map(row => {
    const active = row.id === selectedRowId;
    return (
      <div
        key={row.id}
        onClick={() => setSelectedRowId(row.id)}
        className={`cursor-pointer border-b border-border/50 p-3 transition-colors
          
          ${active 
            ? 'bg-accent/10 border-l-2 border-l-accent' 
            : 'hover:bg-panel2 border-l-2 border-l-transparent'}
        `}
      >
        <div className="grid grid-cols-[2fr_1fr_0.3fr_1.5fr_0.8fr] items-center gap-2">
          <div className="min-w-0">
            <div className={`truncate text-sm font-semibold ${active ? 'text-accent' : 'text-text'}`}>
              {row.sourceSystem}.{row.sourceField}
            </div>
            <div className="mt-0.5 truncate text-xs text-muted">
              {row.commonEntity}
            </div>
          </div>
          <div className="text-sm text-muted">
            {row.sourceType}
          </div>
          <div className="text-center text-muted text-sm">
            →
          </div>
          <div className="truncate text-sm text-text">
            {row.commonField}
          </div>
          <div>
            {pill(row.confidence)}
          </div>
        </div>
      </div>
    );
  })}

  {pageRows.length === 0 && (
    <div className="p-4 text-sm text-muted">
      No rows match your filters.
    </div>
  )}
</div>
<div className="mt-3 flex items-center justify-between text-sm text-text">
  <button
    type="button"
    disabled={!canPrev}
    onClick={() => setCurrentPage(p => p - 1)}
    className="flex items-center gap-1 rounded border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
  >
    <ChevronLeft className="h-4 w-4" /> Prev
  </button>

  <span>{currentPage} of {totalPages}</span>

  <button
    type="button"
    disabled={!canNext}
    onClick={() => setCurrentPage(p => p + 1)}
    className="flex items-center gap-1 rounded border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
  >
    Next <ChevronRight className="h-4 w-4" />
  </button>
</div>
</div>
  );
}
 