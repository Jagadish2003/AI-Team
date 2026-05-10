import React from 'react';
import { OpportunityCandidate, OpportunityTier } from '../../types/analystReview';

function tierBadge(tier: OpportunityTier) {
  const cls =
    tier === 'Quick Win' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
    tier === 'Strategic' ? 'border-amber-500/40 bg-amber-500/10 text-amber-200' :
    'border-red-500/40 bg-red-500/10 text-red-200';
  return <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide ${cls}`}>{tier}</span>;
}

export default function TopQuickWins({
  quickWins,
  selectedId,
  onSelect,
}: {
  quickWins: OpportunityCandidate[];
  selectedId?: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col rounded-xl border border-border bg-panel p-4">
      <div className="flex shrink-0 items-center justify-between">
        <div className="pb-2 text-xl font-semibold text-text">Top Quick Wins</div>
        <div className="text-xs text-muted">Impact - Effort</div>
      </div>

      <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-border bg-bg/20">
        {quickWins.length === 0 ? (
          <div className="px-3 py-4 text-sm text-muted">No quick wins match the current filters.</div>
        ) : (
          quickWins.map((o) => {
            const active = selectedId === o.id;
            return (
              <button
                key={o.id}
                onClick={() => onSelect(o.id)}
                className={`w-full border-b border-border/50 p-3 text-left transition-colors last:border-b-0 ${
                  active ? 'border-l-2 border-l-[#0D55D7] bg-[#0D55D7]/10' : 'hover:bg-panel2'
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className={`text-sm font-semibold leading-snug ${active ? 'text-accent' : 'text-text'}`}>
                      {o.title}
                    </div>
                    <div className="mt-1 text-xs text-muted">{o.category} - Confidence {o.confidence}</div>
                  </div>
                  {tierBadge(o.tier)}
                </div>
                <div className="mt-2 text-xs text-muted">Impact {o.impact}/10 - Effort {o.effort}/10</div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
