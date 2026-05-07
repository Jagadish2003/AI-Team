import React from 'react';
import { EvidenceReview } from '../../types/partialResults';
import { Check, Monitor, X } from 'lucide-react';

interface EvidenceViewerProps {
  evidence: EvidenceReview | null;
  positionLabel: string;
  onPrev: () => void;
  onNext: () => void;
  onApprove: () => void;
  onReject: () => void;
}

const EvidenceViewer: React.FC<EvidenceViewerProps> = ({
  evidence,
  positionLabel,
  onPrev,
  onNext,
  onApprove,
  onReject
}) => {
  const isFinalized = !!evidence && evidence.decision !== 'UNREVIEWED';
  const decisionButtonBase =
    'flex w-full items-center justify-center gap-1.5 rounded-lg border py-2.5 text-xs font-semibold transition-all disabled:cursor-not-allowed disabled:opacity-70';

  return (
    <div className="flex h-full flex-col rounded-xl border border-border bg-panel p-5">
      <div className="flex items-center justify-between border-b border-border pb-4">
        <div className="text-xl font-semibold text-text pb-3">Evidence Viewer</div>
        <Monitor className="h-5 w-5 text-slate-400" />
      </div>

      {!evidence ? (
        <div className="mt-4 text-sm text-muted">Select an evidence snippet to view details.</div>
      ) : (
        <div className="mt-4 flex flex-1 flex-col gap-4">
          <div>
            <div className="mb-1 text-xs text-muted">{evidence.tsLabel}</div>
            <div className="text-sm font-semibold leading-snug text-text">{evidence.title}</div>
          </div>

          <div className="flex items-center gap-3 rounded-lg border border-border bg-bg/40 px-3 py-2 text-xs text-muted">
            <span>
              Source: <span className="font-semibold text-text">{evidence.source}</span>
            </span>
            <span>
              Confidence: <span className="font-semibold text-text">{evidence.confidence}</span>
            </span>
          </div>

          <div className="rounded-lg border border-border bg-bg/40 px-3 py-2 text-xs text-muted">
            Evidence Source Type : {evidence.evidenceType}
          </div>
          <div className="text-sm leading-relaxed text-text">{evidence.snippet}</div>
          <div className="flex flex-col gap-2 text-xs text-muted">
          <div className="flex items-center justify-between rounded border border-border bg-bg/30 px-3 py-2">
            <span>
              Source: <span className="font-semibold text-text">{evidence.source}</span>
            </span>
            <span>
              Type: <span className="font-semibold text-text">{evidence.evidenceType}</span>
            </span>
            <span> Decision: <span className="font-semibold text-text">{evidence.decision}</span></span>  
          </div>
        </div>
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={onApprove}
              disabled={isFinalized}
              className={`${decisionButtonBase}
                ${evidence.decision === 'APPROVED'
                  ? 'border-accent bg-accent text-white shadow-[0_0_0_1px_rgba(13,85,215,0.25)]'
                  : 'border-accent/35 bg-accent/10 text-text hover:border-accent/60 hover:bg-accent/15'
                }`}
            >
              {evidence.decision === 'APPROVED' ? (
                <>
                  <Check size={14} strokeWidth={2.5} />
                  Approved
                </>
              ) : (
                <>
                  <Check size={14} strokeWidth={2.5} />
                  Approve
                </>
              )}
            </button>

            <button
              onClick={onReject}
              disabled={isFinalized}
              className={`${decisionButtonBase}
                ${evidence.decision === 'REJECTED'
                  ? 'border-accent/70 bg-panel2 text-text shadow-[0_0_0_1px_rgba(13,85,215,0.20)]'
                  : 'border-border bg-bg/30 text-muted hover:border-accent/50 hover:bg-panel2 hover:text-text'
                }`}
            >
              {evidence.decision === 'REJECTED' ? (
                <>
                  <X size={14} strokeWidth={2.5} />
                  Rejected
                </>
              ) : (
                <>
                  <X size={14} strokeWidth={2.5} />
                  Reject
                </>
              )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default EvidenceViewer;
