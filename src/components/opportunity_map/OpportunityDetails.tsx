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
      <div className="text-sm font-semibold text-text">Opportunity Details</div>

      {!selected ? (
        <div className="mt-3 text-sm text-muted">Select a bubble to preview details.</div>
      ) : (
        <>
          <div className="mt-3 text-sm font-semibold text-text">{selected.title}</div>

          <div className="mt-2 flex flex-wrap items-center gap-2">
            {tierBadge(selected.tier)}
            {confidenceBadge(selected.confidence)}
            {decisionBadge(selected.decision)}
          </div>

          <div className="mt-2 text-xs text-muted">{selected.category}</div>

          <div className="mt-3 rounded-lg border border-border bg-bg/20 p-3">
            <div className="text-xs font-semibold text-text">Summary</div>
            <div className="mt-2 text-sm text-text">{selected.aiRationale}</div>
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
              className="rounded-md border border-[#00B4B4]/50 bg-[#00B4B4]/10 px-3 py-2 text-sm text-[#00B4B4] hover:bg-[#00B4B4]/20 transition-colors"
            >
              Go to Review
            </button>
          </div>
        </>
      )}
    </div>
  );
}
