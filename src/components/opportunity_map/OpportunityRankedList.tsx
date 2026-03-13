import React from 'react';
import { OpportunityCandidate, OpportunityTier } from '../../types/analystReview';

function tierBadge(tier: OpportunityTier) {
  const cls =
    tier === 'Quick Win' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
    tier === 'Strategic' ? 'border-amber-500/40 bg-amber-500/10 text-amber-200' :
    'border-red-500/40 bg-red-500/10 text-red-200';
  return <span className={`rounded-full border px-2 py-0.5 text-xs ${cls}`}>{tier}</span>;
}

function confidenceBadge(conf: string) {
  const cls =
    conf === 'HIGH'   ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
    conf === 'MEDIUM' ? 'border-amber-500/40 bg-amber-500/10 text-amber-200' :
    'border-red-500/40 bg-red-500/10 text-red-200';
  return <span className={`rounded-full border px-2 py-0.5 text-xs ${cls}`}>{conf}</span>;
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
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-text">Opportunity List</div>
        <div className="text-xs text-muted">Ranked</div>
      </div>

      <div className="mt-3 max-h-[420px] overflow-auto space-y-2">
        {ranked.length === 0 ? (
          <div className="text-sm text-muted">No opportunities match the current filters.</div>
        ) : (
          ranked.map(o => {
            const active = selectedId === o.id;
            return (
              <div
                key={o.id}
                onClick={() => onSelect(o.id)}
                className={`cursor-pointer rounded-lg border p-3 transition-colors
                  ${active
                    ? 'border-accent/60 bg-panel2'
                    : 'border-border bg-bg/20 hover:bg-panel2 hover:border-[#00B4B4]/40'
                  }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-text">{o.title}</div>
                    <div className="mt-1 text-xs text-muted">{o.category}</div>
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    {tierBadge(o.tier)}
                    {confidenceBadge(o.confidence)}
                  </div>
                </div>
                <div className="mt-2 flex items-center justify-between text-xs text-muted">
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
