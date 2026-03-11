import React, { useEffect, useRef } from 'react';
import { OpportunityCandidate, Decision, ReviewAuditEvent } from '../../types/analystReview';

export default function ReasoningOverride({
  opp,
  audit,
  onOverrideText,
  onLockToggle,
  onDecision,
}: {
  opp: OpportunityCandidate | null;
  audit: ReviewAuditEvent[];
  onOverrideText: (v: string) => void;
  onLockToggle: () => void;
  onDecision: (d: Decision) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }, [opp?.override.rationaleOverride]);

  return (
    <div className="flex flex-col rounded-xl border border-border bg-panel overflow-hidden h-full">

      {/* Header */}
      <div className="px-4 py-3 border-b border-border">
        <div className="text-sm font-semibold text-text">Reasoning Override</div>
      </div>

      {/* Scrollable body — flex col so textarea can grow and lock stays pinned */}
      <div className="flex flex-1 flex-col px-4 py-4 gap-4 overflow-hidden">
        {!opp ? (
          <div className="text-xs text-muted">Select an opportunity to review.</div>
        ) : (
          <>
            {/* Approve / Reject */}
            <div className="grid grid-cols-2 gap-2 shrink-0">
              {/* Approve button */}
              <button
                onClick={() => onDecision('APPROVED')}
                className={`flex items-center justify-center gap-1.5 py-2.5 text-xs font-semibold rounded-lg border transition-all
                  ${opp.decision === 'APPROVED'
                    ? 'bg-emerald-500/20 border-emerald-500/60 text-emerald-300'
                    : 'bg-emerald-500/5 border-emerald-500/25 text-emerald-100 hover:bg-emerald-500/15 hover:border-emerald-500/50 hover:text-emerald-300'
                  }`}
              >
                {opp.decision === 'APPROVED' ? (
                  <>
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                    </svg>
                    Approved
                  </>
                ) : (
                  <>
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                    Approve
                  </>
                )}
              </button>

              {/* Reject button */}
              <button
                onClick={() => onDecision('REJECTED')}
                className={`flex items-center justify-center gap-1.5 py-2.5 text-xs font-semibold rounded-lg border transition-all
                  ${opp.decision === 'REJECTED'
                    ? 'bg-red-500/20 border-red-500/60 text-red-300'
                    : 'bg-red-500/5 border-red-500/25 text-red-100 hover:bg-red-500/15 hover:border-red-500/50 hover:text-red-300'
                  }`}
              >
                {opp.decision === 'REJECTED' ? (
                  <>
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                    Rejected
                  </>
                ) : (
                  <>
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                    Reject
                  </>
                )}
              </button>
            </div>

            <div className="border-t border-border shrink-0" />

            {/* Architect Override — grows and scrolls internally when it hits the panel bottom */}
            <div className="flex flex-1 flex-col overflow-hidden min-h-0">
              <div className="text-xs font-semibold text-text mb-2 shrink-0">Architect Override</div>
              <textarea
                ref={textareaRef}
                className="flex-1 w-full rounded-lg border border-border bg-bg/30 text-xs p-3 text-text placeholder:text-muted resize-none leading-relaxed hover:border-[#00B4B4]/50 transition-colors focus:outline-none focus:border-[#00B4B4] focus:ring-2 focus:ring-[#00B4B4]/50 disabled:opacity-50 disabled:hover:border-border overflow-y-auto"
                placeholder="Rewrite rationale in enterprise language…"
                value={opp.override.rationaleOverride}
                onChange={e => onOverrideText(e.target.value)}
                disabled={opp.override.isLocked}
              />
            </div>

            {/* Lock toggle — always pinned at the bottom */}
            <button
              onClick={onLockToggle}
              className={`shrink-0 w-full flex items-center gap-2 px-3 py-2 rounded-lg border text-xs transition-colors
                ${opp.override.isLocked
                  ? 'bg-amber-500/10 border-amber-500/30 text-amber-300'
                  : 'bg-panel2 border-border text-muted hover:text-text'
                }`}
            >
              {opp.override.isLocked ? (
                <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <rect x="3" y="11" width="18" height="11" rx="2" strokeWidth="2" />
                  <path d="M7 11V7a5 5 0 0110 0v4" strokeWidth="2" />
                </svg>
              ) : (
                <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <rect x="3" y="11" width="18" height="11" rx="2" strokeWidth="2" />
                  <path d="M7 11V7a5 5 0 019.9-1" strokeWidth="2" />
                </svg>
              )}
              {opp.override.isLocked ? 'Override locked — click to unlock' : 'Lock override rationale'}
            </button>

            {/* Last updated */}
            {opp.override.updatedAt && (
              <div className="shrink-0 text-xs text-muted">
                Last updated: {new Date(opp.override.updatedAt).toLocaleString()}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}