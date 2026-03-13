import React, { useMemo, useRef, useState } from 'react';
import { OpportunityCandidate, OpportunityTier } from '../../types/analystReview';
import { Search, ChevronRight, ChevronDown } from 'lucide-react';

type SortMode = 'Impact High→Low' | 'Effort Low→High' | 'Confidence High→Low';
type TierFilter = 'All' | OpportunityTier;

// HIGH=green, MEDIUM=amber, LOW=red
function ConfidenceBadge({ value }: { value: string }) {
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

// APPROVED=green, REJECTED=red, UNREVIEWED=muted
function DecisionBadge({ value }: { value: string }) {
  const cls =
    value === 'APPROVED'
      ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300'
      : value === 'REJECTED'
      ? 'border-red-500/50 bg-red-500/15 text-red-300'
      : 'border-border bg-bg/30 text-muted';
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide whitespace-nowrap ${cls}`}>
      {value}
    </span>
  );
}

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
  const [tier, setTier]   = useState<TierFilter>('All');
  const [sort, setSort]   = useState<SortMode>('Impact High→Low');

  const [tierOpen, setTierOpen] = useState(false);
  const [sortOpen, setSortOpen] = useState(false);
  const tierRef = useRef<HTMLDivElement>(null);
  const sortRef = useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (tierRef.current && !tierRef.current.contains(e.target as Node)) setTierOpen(false);
      if (sortRef.current && !sortRef.current.contains(e.target as Node)) setSortOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = [...items];
    if (tier !== 'All') list = list.filter(o => o.tier === tier);
    if (q) list = list.filter(o =>
      o.title.toLowerCase().includes(q) || o.category.toLowerCase().includes(q)
    );
    if (sort === 'Impact High→Low') {
      list.sort((a, b) => b.impact - a.impact);
    } else if (sort === 'Effort Low→High') {
      list.sort((a, b) => a.effort - b.effort);
    } else if (sort === 'Confidence High→Low') {
      const s = (c: string) => c === 'HIGH' ? 3 : c === 'MEDIUM' ? 2 : 1;
      list.sort((a, b) => s(b.confidence) - s(a.confidence));
    }
    return list;
  }, [items, query, tier, sort]);

  const tierOptions: TierFilter[] = ['All', 'Quick Win', 'Strategic', 'Complex'];
  const sortOptions: SortMode[]   = ['Impact High→Low', 'Effort Low→High', 'Confidence High→Low'];
  const tierLabels: Record<TierFilter, string> = {
    All: 'All Tiers', 'Quick Win': 'Quick Win', Strategic: 'Strategic', Complex: 'Complex',
  };
  const sortLabels: Record<SortMode, string> = {
    'Impact High→Low': 'Sort: Impact High→Low',
    'Effort Low→High': 'Sort: Effort Low→High',
    'Confidence High→Low': 'Sort: Confidence High→Low',
  };

  const dropdownBtn =
    'flex items-center gap-2 px-3 py-1.5 rounded-lg border border-border bg-panel2 text-xs text-text hover:border-[#00B4B4]/50 hover:text-[#00B4B4] transition-colors cursor-pointer w-full';
  const dropdownPanel =
    'absolute top-full left-0 z-50 mt-1 w-full rounded-lg border border-border bg-panel shadow-lg overflow-hidden';
  const dropdownItem = (active: boolean) =>
    `px-3 py-2 text-xs cursor-pointer transition-colors ${
      active ? 'bg-[#00B4B4] font-medium text-[#0d1117]' : 'text-text hover:bg-[#00B4B4]/15 hover:text-[#00B4B4]'
    }`;

  return (
    <div className="flex flex-col rounded-xl border border-border bg-panel overflow-hidden h-full">

      {/* Header */}
      <div className="px-4 py-3 border-b border-border flex items-center justify-between shrink-0">
        <div className="text-xl font-semibold text-text">Opportunities</div>
        <div className="text-xs text-muted">{filtered.length} shown</div>
      </div>

      {/* Search */}
      <div className="px-3 py-2 border-b border-border shrink-0">
        <div className="relative">
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search opportunities..."
            className="w-full rounded-md border border-border bg-bg/50 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted hover:border-[#00B4B4]/50 transition-colors focus:outline-none focus:border-[#00B4B4] focus:ring-2 focus:ring-[#00B4B4]/50"
          />
          <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
        </div>
      </div>

      {/* Tier + Sort */}
      <div className="px-3 py-2 border-b border-border flex gap-2 shrink-0">
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

      {/* Cards */}
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
                className={`px-3 py-3 border-b border-border cursor-pointer transition-colors
                  ${active ? 'bg-accent/10 border-l-2 border-l-accent' : 'hover:bg-panel2'}`}
              >
                {/* ── Row 1: title + confidence badge (no icon before title) ── */}
                <div className="flex items-start gap-2">
                  {/* Title + subtitle — takes all available middle space */}
                  <div className="flex-1 min-w-0">
                    <div className={`text-xs font-semibold truncate ${active ? 'text-accent' : 'text-text'}`}>
                      {o.title}
                    </div>
                    <div className="text-xs text-muted truncate mt-0.5">
                      {o.category} · {o.tier}
                    </div>
                  </div>

                  {/* Confidence badge — fixed to right, never moves */}
                  <div className="shrink-0">
                    <ConfidenceBadge value={o.confidence} />
                  </div>
                </div>

                {/* ── Row 2: decision badge LEFT | impact+effort RIGHT | chevron FIXED ──
                    Key fix: use a 3-part layout so chevron occupies its own fixed slot
                    and never displaces impact/effort text regardless of active state */}
                <div className="mt-2 flex items-center gap-2">

                  {/* LEFT: decision badge — fixed width slot */}
                  <div className="w-24 shrink-0">
                    <DecisionBadge value={o.decision} />
                  </div>

                  {/* MIDDLE: impact + effort — flex-1 so it fills space, text always right-aligned */}
                  <div className="flex-1 flex items-center justify-end gap-3 text-[10px] text-muted">
                    <span>
                      Impact <span className="text-text font-medium">{o.impact}/10</span>
                    </span>
                    <span>
                      Effort <span className="text-text font-medium">{o.effort}/10</span>
                    </span>
                  </div>

                  {/* RIGHT: chevron — always occupies same fixed width slot (invisible when not active)
                      This ensures impact/effort NEVER shift when row becomes active */}
                  <div className="w-4 shrink-0 flex items-center justify-end">
                    {active
                      ? <ChevronRight size={12} className="text-accent" />
                      : null
                    }
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Create Opportunity */}
      <div className="border-t border-border shrink-0">
        <button
          onClick={onCreate}
          className="w-full px-4 py-2.5 text-xs text-text bg-panel2 hover:bg-border/40 flex items-center gap-1.5 transition-colors"
        >
          <span className="text-accent font-bold text-sm">+</span>
          <span>Create New Opportunity</span>
          <ChevronRight size={14} className="ml-auto text-muted" />
        </button>
      </div>
    </div>
  );
}