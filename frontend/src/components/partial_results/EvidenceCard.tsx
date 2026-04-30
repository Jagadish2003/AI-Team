import React from 'react';
import { ArrowRight } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import type { OpportunityCandidate } from '../../types/analystReview';
import type { EvidenceReview } from '../../types/partialResults';
import { deriveInterpretation, sourceBadgeClass } from '../../utils/evidenceInterpreter';

function confidenceBadge(confidence: string) {
  const cls =
    confidence === 'HIGH'
      ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300'
      : confidence === 'MEDIUM'
        ? 'border-amber-500/50 bg-amber-500/15 text-amber-300'
        : 'border-red-500/50 bg-red-500/15 text-red-300';

  return (
    <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide ${cls}`}>
      {confidence}
    </span>
  );
}

function decisionBadge(decision: string) {
  const cls =
    decision === 'APPROVED'
      ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300'
      : decision === 'REJECTED'
        ? 'border-red-500/50 bg-red-500/15 text-red-300'
        : 'border-border bg-bg/30 text-muted';

  return (
    <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide ${cls}`}>
      {decision}
    </span>
  );
}

function evidenceTypeBadge(type: string) {
  return (
    <span className="whitespace-nowrap rounded-full border border-border bg-bg/30 px-2 py-0.5 text-[10px] text-text">
      {type}
    </span>
  );
}

export default function EvidenceCard({
  evidence,
  selected,
  onSelect,
  opportunities,
}: {
  evidence: EvidenceReview;
  selected: boolean;
  onSelect: (id: string) => void;
  opportunities: OpportunityCandidate[];
}) {
  const nav = useNavigate();
  const interpretation = deriveInterpretation(evidence);
  const referencingOpps = opportunities.filter((o) => o.evidenceIds?.includes(evidence.id));
  const firstOppId = referencingOpps[0]?.id ?? null;

  const handleCardKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onSelect(evidence.id);
    }
  };

  const handleLinkageClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (firstOppId) {
      nav(`/opportunity-review?oppId=${encodeURIComponent(firstOppId)}`);
    } else {
      nav('/opportunity-review');
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onSelect(evidence.id)}
      onKeyDown={handleCardKeyDown}
      className={[
        'w-full cursor-pointer border-b border-border/50 p-3 text-left transition-colors',
        selected ? 'border-l-2 border-l-[#0D55D7] bg-[#0D55D7]/10' : 'hover:bg-panel2',
      ].join(' ')}
      data-testid={`evidence-card-${evidence.id}`}
      aria-selected={selected}
      aria-label={`Evidence: ${evidence.title}`}
    >
      <div className="flex items-center justify-between gap-3 text-xs text-muted">
        <span>{evidence.tsLabel}</span>
        <div className="flex shrink-0 items-center gap-2">
          {confidenceBadge(evidence.confidence)}
        </div>
      </div>

      <div className={`mt-1 text-sm font-semibold leading-snug ${selected ? 'text-accent' : 'text-text'}`}>
        {evidence.title}
      </div>

      <div className="mt-1 line-clamp-2 text-sm leading-relaxed text-muted">
        {evidence.snippet}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] ${sourceBadgeClass(evidence.source)}`}>
          {evidence.source}
        </span>
        {evidenceTypeBadge(evidence.evidenceType)}
        {decisionBadge(evidence.decision)}
      </div>

      <div className="mt-2 border-l border-border/70 pl-3">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">
          What this means
        </div>
        <div className="mt-1 text-xs leading-relaxed text-text">{interpretation}</div>
      </div>

      {referencingOpps.length > 0 ? (
        <button
          type="button"
          onClick={handleLinkageClick}
          className="mt-2 flex items-center gap-1.5 text-[11px] font-medium text-accent hover:underline"
          data-testid={`evidence-card-linkage-${evidence.id}`}
          aria-label={`View ${referencingOpps.length === 1 ? 'opportunity' : `first of ${referencingOpps.length} opportunities`} in Opportunity Review`}
        >
          <ArrowRight size={11} />
          {referencingOpps.length === 1
            ? 'Referenced by 1 opportunity - View in Opportunity Review'
            : `Referenced by ${referencingOpps.length} opportunities - View first in Opportunity Review`}
        </button>
      ) : (
        <div className="mt-2 text-[11px] text-muted">
          Not yet linked to an opportunity.
        </div>
      )}
    </div>
  );
}
