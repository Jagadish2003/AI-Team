import React from 'react';
import Button from '../common/Button';
import { Confidence } from '../../utils/confidence';
import { Connector } from '../../types/connector';

export default function NextBestSourcePanel({
  confidence,
  recommendedConnectedCount,
  recommendedTotal,
  next,
  onConnectNext
}: {
  confidence: Confidence;
  recommendedConnectedCount: number;
  recommendedTotal: number;
  next: Connector | null;
  onConnectNext: () => void;
}) {
  const step = confidence?.toLowerCase();
  const isLow    = step === 'low';
  const isMedium = step === 'medium';
  const isHigh   = step === 'high';

  return (
    <div className="mt-3 rounded-xl border border-border bg-panel p-4">

      <div className="text-xl font-semibold text-text">Confidence</div>

      {/* ── Low → Medium → High indicator (same style as DiscoveryStartBar) ── */}
      <div className="mt-3 flex items-center text-sm">

        {/* Low */}
        <div className="flex items-center">
          <div className={`h-2.5 w-2.5 rounded-full ${isLow ? 'bg-accent shadow-[0_0_8px_3px_rgba(0,255,200,0.35)]' : 'bg-muted/40'}`} />
          <span className={`ml-2 ${isLow ? 'font-semibold text-text' : 'text-muted'}`}>Low</span>
        </div>

        {/* Line Low → Medium */}
        <div className={`mx-3 h-[1px] flex-1 transition-colors ${isMedium || isHigh ? 'bg-accent/50' : 'bg-border'}`} />

        {/* Medium */}
        <div className="flex items-center">
          <div className={`h-2.5 w-2.5 rounded-full ${isMedium ? 'bg-accent shadow-[0_0_8px_3px_rgba(0,255,200,0.35)]' : 'bg-muted/40'}`} />
          <span className={`ml-2 ${isMedium ? 'font-semibold text-text' : 'text-muted'}`}>Medium</span>
        </div>

        {/* Line Medium → High */}
        <div className={`mx-3 h-[1px] flex-1 transition-colors ${isHigh ? 'bg-accent/50' : 'bg-border'}`} />

        {/* High */}
        <div className="flex items-center">
          <div className={`h-2.5 w-2.5 rounded-full ${isHigh ? 'bg-accent shadow-[0_0_8px_3px_rgba(0,255,200,0.35)]' : 'bg-muted/40'}`} />
          <span className={`ml-2 ${isHigh ? 'font-semibold text-text' : 'text-muted'}`}>High</span>
        </div>

      </div>

      {/* Connected count */}
      <div className="mt-3 text-xs text-muted">
        Connected : <span className="text-text">{recommendedConnectedCount}</span> of <span className="text-text">{recommendedTotal}</span> recommended
      </div>

      {/* Next best source box */}
      <div className="mt-3 rounded-lg border border-border bg-bg/30 p-3">
        {next ? (
          <>
            <div className="text-xs text-muted">Next best source to connect</div>
            <div className="mt-1 text-sm font-semibold text-text">{next.name}</div>
            <div className="mt-1 text-xs text-muted">
              Adds signals from: <span className="text-text">{next.category}</span>
            </div>
            <div className="mt-3">
              <Button className="w-full" onClick={onConnectNext}>
                Connect {next.name}
              </Button>
            </div>
          </>
        ) : (
          <div className="text-sm text-text">All recommended sources connected.</div>
        )}
      </div>

    </div>
  );
}