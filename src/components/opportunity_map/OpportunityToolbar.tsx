import React from 'react';
import { Search } from 'lucide-react';
import { OpportunityTier } from '../../types/analystReview';

export type TierFilter = 'All' | OpportunityTier;
export type ConfidenceFilter = 'All' | 'LOW' | 'MEDIUM' | 'HIGH';
export type DecisionFilter = 'All' | 'UNREVIEWED' | 'APPROVED' | 'REJECTED';

const selectClass =
  'w-full rounded-md border border-border bg-bg/50 px-3 py-2 text-sm text-text appearance-none hover:border-[#00B4B4]/50 transition-colors focus:outline-none focus:border-[#00B4B4] focus:ring-2 focus:ring-[#00B4B4]/50 cursor-pointer';

export default function OpportunityToolbar({
  q, onQ,
  tier, onTier,
  conf, onConf,
  decision, onDecision,
  totalShown,
}: {
  q: string;
  onQ: (v: string) => void;
  tier: TierFilter;
  onTier: (v: TierFilter) => void;
  conf: ConfidenceFilter;
  onConf: (v: ConfidenceFilter) => void;
  decision: DecisionFilter;
  onDecision: (v: DecisionFilter) => void;
  totalShown: number;
}) {
  return (
    <div className="mb-4 rounded-xl border border-border bg-panel p-3">
      <div className="grid grid-cols-1 gap-2 md:grid-cols-[1fr_200px_200px_200px]">
        {/* Search */}
        <div className="relative">
          <input
            value={q}
            onChange={e => onQ(e.target.value)}
            placeholder="Search opportunities…"
            className="w-full rounded-md border border-border bg-bg/50 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted hover:border-[#00B4B4]/50 transition-colors focus:outline-none focus:border-[#00B4B4] focus:ring-2 focus:ring-[#00B4B4]/50 appearance-none"
          />
          <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
        </div>

        <select className={selectClass} value={tier} onChange={e => onTier(e.target.value as TierFilter)}>
          <option value="All">Tier: All</option>
          <option value="Quick Win">Tier: Quick Win</option>
          <option value="Strategic">Tier: Strategic</option>
          <option value="Complex">Tier: Complex</option>
        </select>

        <select className={selectClass} value={conf} onChange={e => onConf(e.target.value as ConfidenceFilter)}>
          <option value="All">Confidence: All</option>
          <option value="HIGH">Confidence: High</option>
          <option value="MEDIUM">Confidence: Medium</option>
          <option value="LOW">Confidence: Low</option>
        </select>

        <select className={selectClass} value={decision} onChange={e => onDecision(e.target.value as DecisionFilter)}>
          <option value="All">Decision: All</option>
          <option value="UNREVIEWED">Decision: Unreviewed</option>
          <option value="APPROVED">Decision: Approved</option>
          <option value="REJECTED">Decision: Rejected</option>
        </select>
      </div>

      <div className="mt-2 text-xs text-muted">
        Showing <span className="font-semibold text-text">{totalShown}</span> opportunities.
      </div>
    </div>
  );
}
