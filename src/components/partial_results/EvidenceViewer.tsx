import React from 'react';
import Button from '../common/Button';
import { EvidenceReview } from '../../types/partialResults';

export default function EvidenceViewer({
  evidence,
  positionLabel,
  onPrev,
  onNext,
  onApprove,
  onReject
}: {
  evidence: EvidenceReview | null;
  positionLabel: string;
  onPrev: () => void;
  onNext: () => void;
  onApprove: () => void;
  onReject: () => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-text">Evidence Viewer</div>
        <span className="text-xs text-muted">⤢</span>
      </div>

      {!evidence ? (
        <div className="mt-4 text-sm text-muted">Select an evidence snippet to view details.</div>
      ) : (
        <>
          <div className="mt-3 flex items-center justify-between text-xs text-muted">
            <span>{evidence.tsLabel}</span>
            <span className="rounded bg-bg/40 px-2 py-0.5">{positionLabel}</span>
          </div>

          <div className="mt-2 text-sm font-semibold text-text">{evidence.title}</div>

          <div className="mt-3 rounded-lg border border-border bg-bg/20 p-3 text-sm text-text">
            {evidence.snippet}
          </div>

          <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted">
            <span className="rounded border border-border bg-bg/30 px-2 py-1">Source: {evidence.source}</span>
            <span className="rounded border border-border bg-bg/30 px-2 py-1">Type: {evidence.evidenceType}</span>
            <span className="rounded border border-border bg-bg/30 px-2 py-1">Confidence: {evidence.confidence}</span>
            <span className="rounded border border-border bg-bg/30 px-2 py-1">Decision: {evidence.decision}</span>
          </div>

          <div className="mt-4 grid grid-cols-2 gap-2">
            <Button onClick={onApprove} variant="primary">Approve</Button>
            <Button onClick={onReject} variant="secondary">Reject</Button>
          </div>

          <div className="mt-4 flex items-center justify-between">
            <Button variant="secondary" onClick={onPrev}>Prev</Button>
            <span className="text-xs text-muted">{positionLabel}</span>
            <Button variant="secondary" onClick={onNext}>Next</Button>
          </div>
        </>
      )}
    </div>
  );
}
