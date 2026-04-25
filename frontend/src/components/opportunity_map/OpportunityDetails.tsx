
import React, { useEffect, useState } from 'react';
import { OpportunityCandidate, OpportunityTier } from '../../types/analystReview';
import { fetchOppEnrichment, OppEnrichment } from '../../api/enrichmentApi';
import { useRunContext } from '../../context/RunContext';

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
  const { runId } = useRunContext();
  const [enrichment, setEnrichment] = useState<OppEnrichment | null>(null);

  useEffect(() => {
    if (!runId || !selected?.id) {
      setEnrichment(null);
      return;
    }
    let cancelled = false;
    fetchOppEnrichment(runId, selected.id)
      .then(data => { if (!cancelled) setEnrichment(data); })
      .catch((err) => {
        if (!cancelled) setEnrichment(null);
        // Fix 5: visible in DevTools during integration — no user-facing UI change
        console.warn('[T7] OpportunityDetails enrichment fetch failed:', err);
      });
    return () => { cancelled = true; };
  }, [runId, selected?.id]);

  // Fix 6: extended fallback chain so panel never silently disappears
  // aiSummary (LLM) → aiRationale (template) → title (last resort) → null (hide panel)
  const summary = enrichment?.aiSummary
    || selected?.aiRationale
    || selected?.title
    || null;
  const isLlm = enrichment?.llmGenerated === true;

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-xl font-semibold text-text pb-2">Opportunity Details</div>
      {!selected ? (
        <div className="mt-3 text-sm text-muted">Select a bubble to preview details.</div>
      ) : (
        <>
          <div className="mt-3 text-sm font-semibold text-text">{selected.title}</div>
          <div className="mt-2 text-xs text-muted">{selected.category}</div>

          {/* Badges — unchanged from original */}
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
            <div className="flex items-center gap-2">
              <span className="w-20 text-xs text-muted">Impact :</span>
              <span className="text-xs text-text">{selected.impact}/10</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="w-20 text-xs text-muted">Effort :</span>
              <span className="text-xs text-text">{selected.effort}/10</span>
            </div>
          </div>

          {/* T7: AI summary panel — only rendered when summary is non-null */}
          {summary && (
            <div className="mt-4">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold text-text">Why this matters</span>
                {isLlm && (
                  <span className="text-xs border border-bg rounded px-1.5 py-0.5 text-text">
                    Claude
                  </span>
                )}
              </div>
              <div className="rounded-lg border border-border bg-bg/30 p-3 text-xs text-text leading-relaxed">
                {summary}
              </div>
            </div>
          )}

          {/* Action buttons — unchanged from original */}
          <div className="mt-4 space-y-2">
            <button
              onClick={onGoToReview}
              className="w-full rounded-md border border-border bg-bg/20 px-3 py-2 text-left text-xs text-text hover:bg-panel2 transition-colors"
            >
              Open in Analyst Review 
            </button>
            <button
              onClick={onViewAnalysis}
              className="w-full rounded-md border border-border bg-bg/20 px-3 py-2 text-left text-xs text-text hover:bg-panel2 transition-colors"
            >
              View Analysis
            </button>
          </div>
        </>
      )}
    </div>
  );
}
