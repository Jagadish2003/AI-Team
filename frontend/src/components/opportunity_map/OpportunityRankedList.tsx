import React from 'react';
import { OpportunityCandidate, OpportunityTier } from '../../types/analystReview';

function tierBadge(tier: OpportunityTier) {
  const cls =
    tier === 'Quick Win' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
    tier === 'Strategic' ? 'border-amber-500/40 bg-amber-500/10 text-amber-200' :
    'border-red-500/40 bg-red-500/10 text-red-200';
  return <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide ${cls}`}>{tier}</span>;
}

function confidenceBadge(conf: string) {
  const cls =
    conf === 'HIGH' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
    conf === 'MEDIUM' ? 'border-amber-500/40 bg-amber-500/10 text-amber-200' :
    'border-red-500/40 bg-red-500/10 text-red-200';
  return <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide ${cls}`}>{conf}</span>;
}

export default function OpportunityRankedList({
  ranked,
  selectedId,
  onSelect,
}: {
  ranked: OpportunityCandidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col rounded-xl border border-border bg-panel p-4">
      <div className="flex shrink-0 items-center justify-between">
        <div className="pb-2 text-xl font-semibold text-text">Opportunity List</div>
        <div className="text-xs text-muted">Ranked</div>
      </div>

      <div className="mt-3 min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-bg/20">
        {ranked.length === 0 ? (
          <div className="px-3 py-4 text-sm text-muted">No opportunities match the current filters.</div>
        ) : (
          ranked.map((o) => {
            const active = selectedId === o.id;
            return (
              <div
                key={o.id}
                onClick={() => onSelect(o.id)}
                className={`cursor-pointer border-b border-border/50 p-3 transition-colors last:border-b-0 ${
                  active ? 'border-l-2 border-l-[#0D55D7] bg-[#0D55D7]/10' : 'hover:bg-panel2'
                }`}
              >
                <div className="min-w-0">
                  <div className={`text-sm font-semibold leading-snug ${active ? 'text-accent' : 'text-text'}`}>
                    {o.title}
                  </div>
                  <div className="mt-1 text-xs text-muted">{o.category}</div>
                </div>

                <div className="mt-2 flex items-center justify-between pb-2">
                  {tierBadge(o.tier)}
                  {confidenceBadge(o.confidence)}
                </div>
                <div className="mt-1 flex items-center justify-between text-xs text-muted">
                  <span>Impact {o.impact}/10</span>
                  <span>Effort {o.effort}/10</span>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
