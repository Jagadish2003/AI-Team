import React from 'react';
import Button from '../common/Button';
import { Confidence } from '../../utils/confidence';

function Slider({ confidence }: { confidence: Confidence }) {
  const position = confidence === 'LOW' ? '12%' : confidence === 'MEDIUM' ? '50%' : '88%';

  return (
    <div className="relative">
      <div className="h-3 rounded-full bg-[#d4d5da]" />
      <span
        className="absolute top-1/2 h-7 w-7 -translate-x-1/2 -translate-y-1/2 rounded-full border border-[#9ea0a7] bg-[#8b8e97]"
        style={{ left: position }}
      />
      <div className="mt-3 flex justify-between px-3 text-[14px] font-medium tracking-[0.12em] text-muted">
        <span>LOW</span>
        <span>MEDIUM</span>
        <span>HIGH</span>
      </div>
    </div>
  );
}

function SourcePill({ label, active }: { label: string; active: boolean }) {
  return (
    <span className={`inline-flex items-center gap-2 rounded-md border px-3 py-2 text-[14px] ${active ? 'border-[#bebfc6] bg-panel text-text' : 'border-border bg-panel2 text-muted'}`}>
      <span className={`h-3 w-3 rounded-sm ${active ? 'bg-[#c5c7cd]' : 'bg-[#b3b6bf]'}`} />
      {label}
    </span>
  );
}

export default function DiscoveryStartBar({
  confidence,
  recommendedConnectedCount,
  recommendedTotal,
  canStart,
  onStart,
  onUpload,
}: {
  confidence: Confidence;
  recommendedConnectedCount: number;
  recommendedTotal: number;
  canStart: boolean;
  onStart: () => void;
  onUpload: () => void;
}) {
  return (
    <div className="fixed bottom-0 left-0 right-0 z-40 px-10 pb-5">
      <div className="mx-auto max-w-[1440px] rounded-2xl border border-border bg-[#ececed] px-8 py-4 shadow-soft">
        <div className="grid grid-cols-1 items-center gap-6 xl:grid-cols-[1.1fr_1.2fr_360px]">
          <div>
            <Slider confidence={confidence} />
            <div className="mt-4">
              <Button variant="secondary" onClick={onUpload} className="min-w-[180px]">
                Upload Files Instead
              </Button>
            </div>
          </div>

          <div>
            <div className="text-[14px] font-medium text-text">
              Connect {recommendedConnectedCount} of {recommendedTotal} recommended <span className="mx-3 text-muted">|</span> ReCx
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <SourcePill label="Jira" active={false} />
              <SourcePill label="Not Connected" active={false} />
              <SourcePill label="Microsoft 365" active={false} />
              <SourcePill label="Not Connected" active={false} />
            </div>
            <div className="mt-4 text-[15px] text-muted">CONFIDENCE: <span className="font-medium text-text">{confidence.charAt(0) + confidence.slice(1).toLowerCase()}</span></div>
          </div>

          <div className="text-right">
            <Button variant="primary" className="h-[78px] w-full text-[18px] font-semibold" onClick={onStart} disabled={!canStart}>
              Start Discovery Run ›
            </Button>
            <div className="mt-4 text-[15px] text-text/90">Connect 1 more source to reach HIGH <span className="ml-1">›</span></div>
          </div>
        </div>
      </div>
    </div>
  );
}
