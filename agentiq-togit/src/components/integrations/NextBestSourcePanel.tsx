import React from 'react';
import Button from '../common/Button';
import { Confidence } from '../../utils/confidence';
import { Connector } from '../../types/connector';

function ConfidenceMeter({ confidence }: { confidence: Confidence }) {
  const position = confidence === 'LOW' ? '18%' : confidence === 'MEDIUM' ? '50%' : '82%';

  return (
    <div>
      <div className="flex items-center gap-3">
        <span className="text-[20px] text-muted">↑</span>
        <div className="relative flex-1 pt-2">
          <div className="h-1 rounded-full bg-border" />
          <span
            className="absolute top-0 h-5 w-5 -translate-x-1/2 rounded-full border border-[#8a8b92] bg-[#6a6b72]"
            style={{ left: position }}
          />
        </div>
      </div>
      <div className="mt-2 flex justify-end gap-8 pr-2 text-[11px] font-medium tracking-[0.12em] text-muted">
        <span>LOW</span>
        <span>MEDIUM</span>
        <span>HIGH</span>
      </div>
    </div>
  );
}

function StatusTag({ label, active }: { label: string; active: boolean }) {
  return (
    <span className={`inline-flex items-center gap-2 text-[13px] ${active ? 'text-text' : 'text-muted'}`}>
      <span className={`h-3 w-3 rounded-sm border ${active ? 'border-[#9d9ea6] bg-[#c9cad0]' : 'border-border bg-panel2'}`} />
      {label}
    </span>
  );
}

export default function NextBestSourcePanel({
  confidence,
  recommendedConnectedCount,
  recommendedTotal,
  next,
  onConnectNext,
}: {
  confidence: Confidence;
  recommendedConnectedCount: number;
  recommendedTotal: number;
  next: Connector | null;
  onConnectNext: () => void;
}) {
  return (
    <section className="rounded-xl border border-border bg-panel p-5 shadow-panel">
      <div className="text-[18px] font-semibold text-text">Confidence</div>
      <div className="mt-4">
        <ConfidenceMeter confidence={confidence} />
      </div>

      <div className="mt-4 text-[16px] text-text/90">Connected {recommendedConnectedCount} of {recommendedTotal} recommended</div>

      <div className="mt-5 rounded-xl border border-border bg-panel2 p-4">
        <div className="flex items-center gap-3 text-[16px] text-text">
          <span className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-panel text-lg text-muted">⚙</span>
          <span className="font-medium">Ready to Start: <span className="text-muted">SERVICE NOW</span></span>
        </div>

        <div className="mt-4 flex flex-wrap gap-x-4 gap-y-2">
          <StatusTag label="Jira" active={false} />
          <StatusTag label="Microsoft 365" active={false} />
          <StatusTag label="Not Connected" active={false} />
        </div>

        {next ? (
          <div className="mt-4">
            <Button variant="secondary" className="w-full" onClick={onConnectNext}>
              Connect {next.name}
            </Button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
