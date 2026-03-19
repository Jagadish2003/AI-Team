import React from 'react';
import Button from '../common/Button';
import { EvidenceReview } from '../../types/partialResults';
import { Monitor } from 'lucide-react';

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
            Entity: {evidence.evidenceType}
          </div>

          <div className="text-sm leading-relaxed text-text">{evidence.snippet}</div>

          <div className="flex flex-wrap gap-2 text-xs text-muted">
            <span className="rounded border border-border bg-bg/30 px-2 py-1">Source: {evidence.source}</span>
            <span className="rounded border border-border bg-bg/30 px-2 py-1">Type: {evidence.evidenceType}</span>
            <span className="rounded border border-border bg-bg/30 px-2 py-1">Decision: {evidence.decision}</span>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={onApprove}
              disabled={isFinalized}
              className={`flex items-center justify-center gap-1.5 py-2.5 text-xs font-semibold rounded-lg border transition-all w-full disabled:cursor-not-allowed
                ${evidence.decision === 'APPROVED'
                  ? 'bg-emerald-500/20 border-emerald-500/60 text-emerald-300'
                  : 'bg-emerald-500/5 border-emerald-500/25 text-emerald-100 hover:bg-emerald-500/15 hover:border-emerald-500/50 hover:text-emerald-300'
                }`}
            >
              {evidence.decision === 'APPROVED' ? (
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
              onClick={onReject}
              disabled={isFinalized}
              className={`flex items-center justify-center gap-1.5 py-2.5 text-xs font-semibold rounded-lg border transition-all w-full disabled:cursor-not-allowed
                ${evidence.decision === 'REJECTED'
                  ? 'bg-red-500/20 border-red-500/60 text-red-300'
                  : 'bg-red-500/5 border-red-500/25 text-red-100 hover:bg-red-500/15 hover:border-red-500/50 hover:text-red-300'
                }`}
            >
              {evidence.decision === 'REJECTED' ? (
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
        </div>
      )}
    </div>
  );
};

export default EvidenceViewer;