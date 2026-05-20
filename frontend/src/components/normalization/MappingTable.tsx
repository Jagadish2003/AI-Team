import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNormalizationContext } from '../../context/NormalizationContext';
import { ChevronDown, ChevronLeft, ChevronRight, Search, ArrowRight } from 'lucide-react';

const PAGE_SIZE = 8;
const tableGridClass =
  'grid-cols-[minmax(0,1.85fr)_minmax(0,1.2fr)_40px_minmax(0,1.25fr)_96px]';

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

  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide whitespace-nowrap ${cls}`}>
      {value}
    </span>
  );
}

export default function MappingTable() {
  const {
    filteredRows, selectedRowId, setSelectedRowId,
    search, setSearch, sortMode, setSortMode,
    activeTab, setActiveTab, counts,
  } = useNormalizationContext();

  const [currentPage, setCurrentPage] = useState(1);
  const [sortOpen, setSortOpen] = useState(false);
  const sortRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (sortRef.current && !sortRef.current.contains(e.target as Node)) {
        setSortOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  useEffect(() => {
    setCurrentPage(1);
  }, [activeTab, search, sortMode, filteredRows.length]);

  const totalPages = Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE));

  const pageRows = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE;
    return filteredRows.slice(start, start + PAGE_SIZE);
  }, [filteredRows, currentPage]);

  useEffect(() => {
    if (pageRows.length === 0) return;
    if (!pageRows.some(row => row.id === selectedRowId)) {
      setSelectedRowId(pageRows[0].id);
    }
  }, [pageRows, selectedRowId, setSelectedRowId]);

  const canPrev = currentPage > 1;
  const canNext = currentPage < totalPages;

  const tabButton = (id: Tab, label: string) => (
    <button
      type="button"
      onClick={() => setActiveTab(id)}
      className={`w-full rounded-md border px-3 py-2.5 text-sm font-semibold transition ${
        activeTab === id
          ? 'border-accent/60 bg-panel2 text-text'
          : 'border-border bg-bg/20 text-muted hover:bg-panel2 hover:text-text'
      }`}
    >
      {label} ({counts[id]})
    </button>
  );

  return (
    <div className="mapping-table-panel flex h-full flex-col rounded-xl border border-border bg-panel p-4">
      
      {/* Tabs + Search */}
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-3 gap-3">
          {tabButton('MAPPED', 'Mapped')}
          {tabButton('UNMAPPED', 'Unmapped')}
          {tabButton('AMBIGUOUS', 'Ambiguous')}
        </div>

        <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(0,1.8fr)_minmax(220px,1fr)]">
          
          {/* Search */}
          <div className="relative">
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search fields..."
              className="w-full rounded-md border border-border bg-bg/30 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted hover:bg-bg/50 hover:border-accent/50 transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50"
            />
            <Search className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
          </div>

          {/* Sort */}
          <div className="relative" ref={sortRef}>
            <button
              type="button"
              onClick={() => setSortOpen(v => !v)}
              className="flex w-full min-w-0 items-center gap-2 rounded-md border border-border bg-bg/30 px-3 py-2 text-sm text-text hover:bg-bg/50 hover:border-accent/50"
            >
              <span className="min-w-0 flex-1 truncate text-left">Sort: {sortMode}</span>
              <ChevronDown size={14} className={`transition ${sortOpen ? 'rotate-180' : ''}`} />
            </button>

            {sortOpen && (
              <div className="absolute right-0 z-50 mt-1 w-full rounded-lg border border-border bg-panel shadow-lg">
                {sortOptions.map(option => (
                  <div
                    key={option}
                    onClick={() => { setSortMode(option); setSortOpen(false); }}
                    className={`cursor-pointer px-4 py-2 text-sm ${
                      option === sortMode
                        ? 'bg-accent text-bg'
                        : 'text-text hover:bg-accent/10'
                    }`}
                  >
                    {option}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Header */}
      <div className={`mt-3 grid ${tableGridClass} items-center gap-2 rounded-t-lg border border-border bg-bg/20 px-4 py-2 text-xs font-semibold text-text`}>
        <div className="min-w-0 pl-2">Source Field</div>
        <div className="min-w-0 pl-2">Source Type</div>
        <div className="flex justify-center">
          <ArrowRight className="h-4 w-4 text-muted" />
        </div>
        <div className="min-w-0 pl-2">Common Field</div>
        <div className="min-w-0 pl-2">Confidence</div>
      </div>

      {/* Rows */}
      <div className="flex flex-col flex-1 overflow-y-auto rounded-b-lg border border-t-0 border-border bg-bg/20">
        {pageRows.map(row => {
          const active = row.id === selectedRowId;

          return (
            <div
              key={row.id}
              onClick={() => setSelectedRowId(row.id)}
              className={`cursor-pointer border-b border-border/50 px-4 py-3 ${
                active
                  ? 'bg-accent/10 border-l-2 border-l-accent'
                  : 'hover:bg-panel2 border-l-2 border-l-transparent'
              }`}
            >
              <div className={`grid ${tableGridClass} items-center gap-2`}>
                
                <div className="min-w-0 pl-2">
                  <div
                    className={`break-all text-sm font-semibold leading-snug ${active ? 'text-accent' : 'text-text'}`}
                    title={`${row.sourceSystem}.${row.sourceField}`}
                  >
                    {row.sourceSystem}.{row.sourceField}
                  </div>
                  <div className="mt-1 break-all text-xs leading-snug text-muted">
                    {row.commonEntity}
                  </div>
                </div>

                <div className="min-w-0 break-all pl-2 text-sm leading-snug text-muted" title={row.sourceType}>
                  {row.sourceType}
                </div>

                <div className="flex justify-center text-muted">
                  <ArrowRight className="h-4 w-4" />
                </div>

                <div className="min-w-0 break-all pl-2 text-sm leading-snug text-text" title={row.commonField}>
                  {row.commonField}
                </div>

                <div className="flex min-w-0 justify-start pl-2">
                  {pill(row.confidence)}
                </div>

              </div>
            </div>
          );
        })}
      </div>

      {/* Pagination */}
      <div className="mt-3 flex items-center justify-between text-sm text-text">
        <button
          disabled={!canPrev}
          onClick={() => setCurrentPage(p => p - 1)}
          className="flex items-center gap-1 rounded border border-accent/20 bg-accent/5 px-4 py-2 text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:opacity-40"
        >
          <ChevronLeft className="h-4 w-4" /> Prev
        </button>

        <span>{currentPage} of {totalPages}</span>

        <button
          disabled={!canNext}
          onClick={() => setCurrentPage(p => p + 1)}
          className="flex items-center gap-1 rounded border border-accent/20 bg-accent/5 px-4 py-2 text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:opacity-40"
        >
          Next <ChevronRight className="h-4 w-4" />
        </button>
      </div>

    </div>
  );
}
