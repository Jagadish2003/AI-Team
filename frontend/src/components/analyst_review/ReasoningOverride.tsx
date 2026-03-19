import React, { useRef } from 'react';
import { OpportunityCandidate, Decision, ReviewAuditEvent } from '../../types/analystReview';

export default function ReasoningOverride({
  opp,
  audit,
  onOverrideText,
  onOverrideReason,
  onSave,
  onViewEvidence,
  onLockToggle,
  onDecision,
}: {
  opp: OpportunityCandidate | null;
  audit: ReviewAuditEvent[];
  onOverrideText: (v: string) => void;
  onOverrideReason: (v: string) => void;
  onSave: () => void;
  onViewEvidence: () => void;
  onLockToggle: () => void;
  onDecision: (d: Decision) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isDecisionFinalized = !!opp && opp.decision !== 'UNREVIEWED';

  return (
    <div className="flex flex-col rounded-xl border border-border bg-panel overflow-hidden h-full">

      <style>{`
        .reason-scroll::-webkit-scrollbar { width: 14px; }
        .reason-scroll::-webkit-scrollbar-track { background: #2c2c2c; border-radius: 0; }
        .reason-scroll::-webkit-scrollbar-thumb { background: #9f9f9f; border-radius: 8px; min-height: 40px; border: 3px solid #2c2c2c; }
        .reason-scroll::-webkit-scrollbar-thumb:hover { background: #b0b0b0; }
        .reason-scroll::-webkit-scrollbar-button { background: #2c2c2c; display: block; height: 14px; }
        .reason-scroll::-webkit-scrollbar-button:vertical:start:decrement {
          background: #2c2c2c url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5' viewBox='0 0 8 5'%3E%3Cpath d='M4 0L0 5h8z' fill='%239f9f9f'/%3E%3C/svg%3E") no-repeat center;
        }
        .reason-scroll::-webkit-scrollbar-button:vertical:end:increment {
          background: #2c2c2c url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5' viewBox='0 0 8 5'%3E%3Cpath d='M4 5L0 0h8z' fill='%239f9f9f'/%3E%3C/svg%3E") no-repeat center;
        }
        .reason-scroll::-webkit-scrollbar-button:vertical:start:increment,
        .reason-scroll::-webkit-scrollbar-button:vertical:end:decrement { display: none; }
      `}</style>

      <div className="px-4 py-3 border-b border-border shrink-0">
        <div className="text-xl font-semibold text-text">Reasoning Override</div>
      </div>

      <div className="reason-scroll flex flex-col px-4 py-4 gap-4 overflow-y-auto flex-1">
        {!opp ? (
          <div className="text-xs text-muted">Select an opportunity to review.</div>
        ) : (
          <div className="flex flex-col gap-4">

            <div className="grid grid-cols-2 gap-2 shrink-0">
              <button
                type="button"
                onClick={() => onDecision('APPROVED')}
                disabled={isDecisionFinalized}
                className={`flex items-center justify-center gap-1.5 py-2.5 text-xs font-semibold rounded-lg border transition-all w-full disabled:cursor-not-allowed
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

              <button
                type="button"
                onClick={() => onDecision('REJECTED')}
                disabled={isDecisionFinalized}
                className={`flex items-center justify-center gap-1.5 py-2.5 text-xs font-semibold rounded-lg border transition-all w-full disabled:cursor-not-allowed
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

            <div
              className="shrink-0 h-[20px] text-xs text-muted flex items-center transition-opacity duration-200"
              style={{ opacity: opp.override.updatedAt ? 1 : 0 }}
            >
              {opp.override.updatedAt &&
                `Last updated: ${new Date(opp.override.updatedAt).toLocaleString()}`}
            </div>

            <div className="border-t border-border shrink-0" />

            <div className="flex flex-col shrink-0">
              <div className="text-xs font-semibold text-text mb-2">Architect Override</div>
              <textarea
                ref={textareaRef}
                className="w-full rounded-lg border border-border bg-bg/30 text-xs p-3 text-text 
                  placeholder:text-muted leading-relaxed hover:border-[#00B4B4]/50 
                  transition-colors focus:outline-none focus:border-[#00B4B4] 
                  focus:ring-2 focus:ring-[#00B4B4]/50 disabled:opacity-50 
                  resize-none overflow-y-auto reason-scroll"
                style={{ maxHeight: '200px', minHeight: '124px' }}
                placeholder="Rewrite rationale in enterprise language…"
                value={opp.override.rationaleOverride ?? ''}
                onChange={e => onOverrideText(e.target.value)}
                disabled={opp.override.isLocked}
              />
            </div>

            <div className="flex flex-col rounded-lg border border-border bg-bg/20 p-3 shrink-0">
              <div className="text-xs font-semibold text-text mb-2">Override reason</div>
              <input
                type="text"
                className="w-full rounded-md border border-border bg-bg/30 px-3 py-2 text-sm text-text placeholder:text-muted 
                focus:outline-none hover:border-[#00B4B4]/50 focus:border-[#00B4B4] focus:ring-2 focus:ring-[#00B4B4]/50 disabled:opacity-50"
                placeholder="Why are we overriding the AI rationale?"
                value={opp.override.overrideReason ?? ''}
                onChange={e => onOverrideReason(e.target.value)}
                disabled={opp.override.isLocked}
              />
            </div>

            <div className="grid grid-cols-2 gap-2 shrink-0">
              <button
                type="button"
                onClick={onSave}
                disabled={opp.override.isLocked}
                className="py-2 rounded-lg border border-border bg-bg/70 text-xs font-semibold 
                text-text hover:bg-panel2  hover:border-[#00B4B4]/50 disabled:opacity-50 transition-colors"
              >
                Save Override
              </button>

              <button
                type="button"
                onClick={onViewEvidence}
                className="py-2 rounded-lg border border-border bg-bg/70 text-xs font-semibold 
                text-text hover:bg-panel2 hover:border-[#00B4B4]/50 transition-colors"
              >
                View Evidence
              </button>
            </div>

            <button
              type="button"
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
          </div>
        )}
      </div>
    </div>
  );
}