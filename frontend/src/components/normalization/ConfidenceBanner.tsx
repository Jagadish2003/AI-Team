import React from 'react';
import { ConfidenceExplanation } from '../../types/normalization';
import { ChevronRight } from 'lucide-react';

export default function ConfidenceBanner({ conf, onAction }: { conf: ConfidenceExplanation; onAction: () => void; }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm text-text">
        <span className="font-semibold">Confidence: {conf.level}</span> — {conf.why}.
        {conf.missingSignals.length > 0 && (
          <span className="text-muted"> Missing: {conf.missingSignals.join(', ')}.</span>
        )}
      </div>
      <div className="mt-3 flex items-center justify-between">
        <div className="text-xs text-muted">This explains why confidence is not High yet.</div>
        <button className="flex items-center gap-2 text-sm font-semibold text-text underline underline-offset-4 hover:opacity-90" onClick={onAction}>
          {conf.nextAction}
          <ChevronRight className="h-4 w-4 text-text flex-shrink-0" />
        </button>
      </div>
    </div>
  );
}