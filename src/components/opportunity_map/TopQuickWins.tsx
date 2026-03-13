import React from 'react';
import { OpportunityCandidate, OpportunityTier } from '../../types/analystReview';

function tierBadge(tier: OpportunityTier) {
  const cls =
    tier === 'Quick Win' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
    tier === 'Strategic' ? 'border-amber-500/40 bg-amber-500/10 text-amber-200' :
    'border-red-500/40 bg-red-500/10 text-red-200';
  return <span className={`rounded-full border px-2 py-0.5 text-xs ${cls}`}>{tier}</span>;
}

export default function TopQuickWins({
  quickWins,
  onSelect,
}: {
  quickWins: OpportunityCandidate[];
  onSelect: (id: string) => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-text">Top Quick Wins</div>
        <div className="text-xs text-muted">Impact − Effort</div>
      </div>

      <div className="mt-3 space-y-2">
        {quickWins.length === 0 ? (
          <div className="text-sm text-muted">No quick wins match the current filters.</div>
        ) : (
          quickWins.map(o => (
            <button
              key={o.id}
              onClick={() => onSelect(o.id)}
              className="w-full rounded-lg border border-border bg-bg/20 p-3 text-left hover:bg-panel2 hover:border-[#00B4B4]/40 transition-colors"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-text">{o.title}</div>
                  <div className="mt-1 text-xs text-muted">{o.category} · Confidence {o.confidence}</div>
                </div>
                {tierBadge(o.tier)}
              </div>
              <div className="mt-2 text-xs text-muted">Impact {o.impact}/10 · Effort {o.effort}/10</div>
            </button>
          ))
        )}
      </div>
    </div>
  );
}
