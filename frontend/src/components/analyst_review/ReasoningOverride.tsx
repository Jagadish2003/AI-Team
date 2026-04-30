import React, { useEffect, useRef, useState } from 'react';
import { OpportunityCandidate, ReviewAuditEvent } from '../../types/analystReview';
import type { Decision } from '../../types/common';

export default function ReasoningOverride({
  opp,
  audit,
  onSave,
  onViewEvidence,
  onDecision,
}: {
  opp: OpportunityCandidate | null;
  audit: ReviewAuditEvent[];
  onSave: (rationaleOverride: string, overrideReason: string, isLocked: boolean) => void;
  onViewEvidence: () => void;
  onDecision: (d: Decision) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [rationaleOverride, setRationaleOverride] = useState(opp?.override.rationaleOverride ?? '');
  const [overrideReason, setOverrideReason] = useState(opp?.override.overrideReason ?? '');
  const [isLocked, setIsLocked] = useState(opp?.override.isLocked ?? false);

  useEffect(() => {
    setRationaleOverride(opp?.override.rationaleOverride ?? '');
    setOverrideReason(opp?.override.overrideReason ?? '');
    setIsLocked(opp?.override.isLocked ?? false);
  }, [opp?.id]);

  const isDecisionFinalized = !!opp && opp.decision !== 'UNREVIEWED';
  const relevantAuditCount = opp ? audit.filter((a) => !a.opportunityId || a.opportunityId === opp.id).length : 0;

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="flex items-center justify-between">
        <div className="pb-2 text-xl font-semibold text-text">Reasoning Override</div>
        <div className="text-xs text-muted">{relevantAuditCount} audit item(s)</div>
      </div>

      {!opp ? (
        <div className="mt-3 rounded-lg border border-border bg-bg/20 px-3 py-4 text-sm text-muted">
          Select an opportunity to review.
        </div>
      ) : (
        <div className="mt-3 space-y-3">
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => onDecision('APPROVED')}
              disabled={isDecisionFinalized}
              className={`flex w-full items-center justify-center gap-1.5 rounded-md border px-3 py-2.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${
                opp.decision === 'APPROVED'
                  ? 'border-emerald-500/60 bg-emerald-500/20 text-emerald-300'
                  : 'border-emerald-500/25 bg-emerald-500/5 text-emerald-100 hover:border-emerald-500/50 hover:bg-emerald-500/15 hover:text-emerald-300'
              }`}
            >
              {opp.decision === 'APPROVED' ? 'Approved' : 'Approve'}
            </button>

            <button
              type="button"
              onClick={() => onDecision('REJECTED')}
              disabled={isDecisionFinalized}
              className={`flex w-full items-center justify-center gap-1.5 rounded-md border px-3 py-2.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${
                opp.decision === 'REJECTED'
                  ? 'border-red-500/60 bg-red-500/20 text-red-300'
                  : 'border-red-500/25 bg-red-500/5 text-red-100 hover:border-red-500/50 hover:bg-red-500/15 hover:text-red-300'
              }`}
            >
              {opp.decision === 'REJECTED' ? 'Rejected' : 'Reject'}
            </button>
          </div>

          {opp.override.updatedAt && (
            <div className="text-xs text-muted">
              Last updated: {new Date(opp.override.updatedAt).toLocaleString()}
            </div>
          )}

          <div>
            <div className="mb-2 text-xs font-semibold text-text">Override rationale</div>
            <textarea
              ref={textareaRef}
              className="h-36 w-full resize-none rounded-md border border-border bg-bg/30 p-3 text-sm leading-relaxed text-text placeholder:text-muted transition-colors hover:border-accent/50 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/50 disabled:opacity-50"
              placeholder="Rewrite rationale in enterprise language..."
              value={rationaleOverride}
              onChange={(e) => setRationaleOverride(e.target.value)}
              disabled={isLocked}
            />
          </div>

          <div>
            <div className="mb-2 text-xs font-semibold text-text">Override reason</div>
            <input
              type="text"
              className="w-full rounded-md border border-border bg-bg/30 px-3 py-2 text-sm text-text placeholder:text-muted transition-colors hover:border-accent/50 focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/50 disabled:opacity-50"
              placeholder="Why are we overriding the AI rationale?"
              value={overrideReason}
              onChange={(e) => setOverrideReason(e.target.value)}
              disabled={isLocked}
            />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => onSave(rationaleOverride, overrideReason, isLocked)}
              disabled={isLocked}
              className="rounded-md border border-border bg-bg/40 px-3 py-2.5 text-sm font-medium text-text transition hover:bg-panel2 hover:border-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save Override
            </button>

            <button
              type="button"
              onClick={onViewEvidence}
              className="rounded-md border border-border bg-bg/40 px-3 py-2.5 text-sm font-medium text-text transition hover:bg-panel2 hover:border-accent/50"
            >
              View Evidence
            </button>
          </div>

          <button
            type="button"
            onClick={() => setIsLocked((locked) => !locked)}
            className={`w-full rounded-md border px-3 py-2.5 text-xs font-medium transition ${
              isLocked
                ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
                : 'border-border bg-bg/30 text-muted hover:bg-panel2 hover:text-text'
            }`}
          >
            {isLocked ? 'Override locked - click to unlock' : 'Lock override rationale'}
          </button>
        </div>
      )}
    </div>
  );
}
