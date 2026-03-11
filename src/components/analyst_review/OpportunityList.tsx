import React, { useMemo, useRef, useState } from 'react';
import { OpportunityCandidate, OpportunityTier } from '../../types/analystReview';
import { Search, ChevronRight, ChevronDown } from 'lucide-react';

type SortMode = 'Priority' | 'Impact High→Low' | 'Effort Low→High';
type TierFilter = 'All' | OpportunityTier;

export default function OpportunityList({
  items,
  selectedId,
  onSelect,
  onCreate,
}: {
  items: OpportunityCandidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
}) {
  const [query, setQuery] = useState('');
  const [tier, setTier] = useState<TierFilter>('All');
  const [sort, setSort] = useState<SortMode>('Priority');

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = [...items];

    if (tier !== 'All') {
      list = list.filter(o => o.tier === tier);
    }
    if (q) {
      list = list.filter(o =>
        o.title.toLowerCase().includes(q) ||
        o.category.toLowerCase().includes(q)
      );
    }
    if (sort === 'Impact High→Low') {
      list.sort((a, b) => b.impact - a.impact);
    } else if (sort === 'Effort Low→High') {
      list.sort((a, b) => a.effort - b.effort);
    } else {
      list.sort((a, b) => (b.impact - b.effort) - (a.impact - a.effort));
    }
    return list;
  }, [items, query, tier, sort]);

  const decisionIcon = (d: string) => {
    if (d === 'APPROVED') return (
      <span className="shrink-0 w-4 h-4 rounded-full bg-emerald-500/20 border border-emerald-500/60 flex items-center justify-center">
        <svg className="w-2.5 h-2.5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
        </svg>
      </span>
    );
    if (d === 'REJECTED') return (
      <span className="shrink-0 w-4 h-4 rounded-full bg-red-500/20 border border-red-500/60 flex items-center justify-center">
        <svg className="w-2.5 h-2.5 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </span>
    );
    return <span className="shrink-0 w-4 h-4 rounded-full border border-border bg-bg/30" />;
  };

  const [tierOpen, setTierOpen] = useState(false);
  const [sortOpen, setSortOpen] = useState(false);
  const tierRef = useRef<HTMLDivElement>(null);
  const sortRef = useRef<HTMLDivElement>(null);

  // Close dropdowns on outside click
  React.useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (tierRef.current && !tierRef.current.contains(e.target as Node)) setTierOpen(false);
      if (sortRef.current && !sortRef.current.contains(e.target as Node)) setSortOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const tierOptions: TierFilter[] = ['All', 'Quick Win', 'Strategic', 'Complex'];
  const sortOptions: SortMode[] = ['Priority', 'Impact High→Low', 'Effort Low→High'];
  const tierLabels: Record<TierFilter, string> = { All: 'All Tiers', 'Quick Win': 'Quick Win', Strategic: 'Strategic', Complex: 'Complex' };
  const sortLabels: Record<SortMode, string> = { Priority: 'Sort: Priority', 'Impact High→Low': 'Impact ↓', 'Effort Low→High': 'Effort ↑' };

  const dropdownBtn = "flex items-center gap-2 px-3 py-1.5 rounded-lg border border-border bg-panel2 text-xs text-text hover:border-[#00B4B4]/50 hover:text-[#00B4B4] transition-colors cursor-pointer w-full";
  const dropdownPanel = "absolute top-full left-0 z-50 mt-1 w-full rounded-lg border border-border bg-panel shadow-lg overflow-hidden";
  const dropdownItem = (active: boolean) =>
    `px-3 py-2 text-xs cursor-pointer transition-colors ${active ? 'bg-[#00B4B4] font-medium text-[#0d1117]' : 'text-text hover:bg-[#00B4B4]/15 hover:text-[#00B4B4]'}`;

  return (
    <div className="flex flex-col rounded-xl border border-border bg-panel overflow-hidden h-full">

      {/* Header */}
      <div className="px-4 py-3 border-b border-border flex items-center justify-between shrink-0">
        <div className="text-sm font-semibold text-text">Opportunities</div>
        <div className="text-xs text-muted">{filtered.length} shown</div>
      </div>

      {/* Search */}
      <div className="px-3 py-2 border-b border-border shrink-0">
        <div className="relative">
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search opportunities..."
            className="w-full rounded-md border border-border bg-bg/50 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted hover:border-[#00B4B4]/50 transition-colors focus:outline-none focus:border-[#00B4B4] focus:ring-2 focus:ring-[#00B4B4]/50 appearance-none"
          />
          <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
        </div>
      </div>

      {/* Tier + Sort */}
      <div className="px-3 py-2 border-b border-border flex gap-2 shrink-0">
        {/* Tier dropdown */}
        <div ref={tierRef} className="relative flex-1">
          <button onClick={() => { setTierOpen(o => !o); setSortOpen(false); }} className={dropdownBtn}>
            <span className="flex-1 text-left truncate">{tierLabels[tier]}</span>
            <ChevronDown size={13} className={`shrink-0 transition-transform ${tierOpen ? 'rotate-180' : ''}`} />
          </button>
          {tierOpen && (
            <div className={dropdownPanel}>
              {tierOptions.map(o => (
                <div key={o} onClick={() => { setTier(o); setTierOpen(false); }} className={dropdownItem(tier === o)}>
                  {tierLabels[o]}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Sort dropdown */}
        <div ref={sortRef} className="relative flex-1">
          <button onClick={() => { setSortOpen(o => !o); setTierOpen(false); }} className={dropdownBtn}>
            <span className="flex-1 text-left truncate">{sortLabels[sort]}</span>
            <ChevronDown size={13} className={`shrink-0 transition-transform ${sortOpen ? 'rotate-180' : ''}`} />
          </button>
          {sortOpen && (
            <div className={dropdownPanel}>
              {sortOptions.map(o => (
                <div key={o} onClick={() => { setSort(o); setSortOpen(false); }} className={dropdownItem(sort === o)}>
                  {sortLabels[o]}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* List — flex-1 ensures it fills remaining height and scrolls */}
      <div className="flex-1 overflow-y-auto min-h-0">
        {filtered.length === 0 ? (
          <div className="px-4 py-8 text-xs text-muted text-center">No opportunities match your filters.</div>
        ) : (
          filtered.map(o => {
            const active = o.id === selectedId;
            return (
              <div
                key={o.id}
                onClick={() => onSelect(o.id)}
                className={`flex items-center gap-2.5 px-3 py-2.5 border-b border-border cursor-pointer transition-colors
                  ${active ? 'bg-accent/10 border-l-2 border-l-accent' : 'hover:bg-panel2'}`}
              >
                {decisionIcon(o.decision)}

                <div className="flex-1 min-w-0">
                  <div className={`text-xs truncate font-medium ${active ? 'text-accent' : 'text-text'}`}>
                    {o.title}
                  </div>
                  <div className="text-xs text-muted truncate">{o.category} · {o.tier}</div>
                </div>

                {active && (
                  <ChevronRight size={12} className="text-accent shrink-0" />
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Create Opportunity — pinned at bottom */}
      <div className="border-t border-border shrink-0">
        <button
          onClick={onCreate}
          className="w-full px-4 py-2.5 text-xs text-text bg-panel2 hover:bg-border/40 flex items-center gap-1.5 transition-colors border-b border-border"
        >
          <span className="text-accent font-bold text-sm">+</span>
          <span>Create New Opportunity</span>
          <ChevronRight size={14} className="ml-auto text-muted" />
        </button>
      </div>
    </div>
  );
}