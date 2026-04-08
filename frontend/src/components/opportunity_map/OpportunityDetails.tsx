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

function decisionBadge(dec: string) {
  const cls =
    dec === 'APPROVED' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' :
    dec === 'REJECTED' ? 'border-red-500/40 bg-red-500/10 text-red-200' :
    'border-border bg-bg/20 text-muted';
  return <span className={`rounded-full border px-2 py-0.5 text-xs ${cls}`}>{dec}</span>;
}

export default function OpportunityDetails({
  selected,
  onViewAnalysis,
  onGoToReview,
}: {
  selected: OpportunityCandidate | null;
  onViewAnalysis: () => void;
  onGoToReview: () => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-xl font-semibold text-text pb-2">Opportunity Details</div>
      {!selected ? (
        <div className="mt-3 text-sm text-muted">Select a bubble to preview details.</div>
      ) : (
        <>
          <div className="mt-3 text-sm font-semibold text-text">{selected.title}</div>
          <div className="mt-2 text-xs text-muted">{selected.category}</div>

          {/* Labeled badges */}
          <div className="mt-3 space-y-2">
            <div className="flex items-center gap-2">
              <span className="w-20 text-xs text-muted">Tier :</span>
              {tierBadge(selected.tier)}
            </div>
            <div className="flex items-center gap-2">
              <span className="w-20 text-xs text-muted">Confidence :</span>
              {confidenceBadge(selected.confidence)}
            </div>
            <div className="flex items-center gap-2">
              <span className="w-20 text-xs text-muted">Status :</span>
              {decisionBadge(selected.decision)}
            </div>
          </div>

          <div className="mt-3 rounded-lg border border-border bg-bg/20 p-3">
            <div className="text-xs font-semibold text-text">Summary</div>
              <div
                className="mt-2 text-sm text-text overflow-y-auto"
                style={{ height: '3.75rem' }}
              >
                {selected.override?.rationaleOverride?.trim()
                  ? selected.override.rationaleOverride
                  : selected.aiRationale}
              </div>
            </div>

          <div className="mt-3 grid grid-cols-2 gap-2">
            <button
              onClick={onViewAnalysis}
              className="rounded-md border border-border bg-bg/20 px-3 py-2 text-sm text-text hover:bg-panel2 hover:border-[#00B4B4]/40 transition-colors"
            >
              View Analysis
            </button>
            <button
              onClick={onGoToReview}
             className="rounded-md border border-border bg-bg/20 px-3 py-2 text-sm text-text hover:bg-panel2 hover:border-[#00B4B4]/40 transition-colors"
            >
              Go to Review
            </button>
          </div>
        </>
      )}
    </div>
  );
}