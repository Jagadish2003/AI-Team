import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useAnalystReviewContext } from '../../context/AnalystReviewContext';
import { useRunContext } from '../../context/RunContext';
import { OpportunityCandidate } from '../../types/analystReview';

interface TopQuickWinsProps {
  quickWins: OpportunityCandidate[];
}

export default function TopQuickWins({ quickWins }: TopQuickWinsProps) {
  const nav = useNavigate();
  const { select } = useAnalystReviewContext();
  const { runId } = useRunContext();

  const handleOpenOpportunity = (id: string) => {
    select(id);
    nav(runId ? `/opportunity-map?runId=${runId}` : '/opportunity-map');
  };

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-semibold text-text">Top Quick Wins</div>
      <div className="mt-3 space-y-2">
        {quickWins.map(o => (
          <button
            key={o.id}
            className="w-full rounded-md border border-border bg-bg/20 px-3 py-2 text-left hover:bg-panel2"
            onClick={() => handleOpenOpportunity(o.id)}
          >
            <div className="text-sm font-semibold text-text">{o.title}</div>
            <div className="mt-1 text-xs text-muted">
              {o.category} · Impact {o.impact}/10 · Effort {o.effort}/10
            </div>
          </button>
        ))}
      </div>
      <div className="mt-2 text-xs text-muted">
        Opens Opportunity Map and pre-selects the same opportunity.
      </div>
    </div>
  );
}
