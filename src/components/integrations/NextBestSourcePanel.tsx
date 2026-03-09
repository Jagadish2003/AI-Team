import React from 'react';
import Button from '../common/Button';
import { Confidence } from '../../utils/confidence';
import { Connector } from '../../types/connector';

function Step({ label, active }: { label: string; active: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <div className={`h-2.5 w-2.5 rounded-full ${active ? 'bg-accent' : 'bg-border'}`} />
      <div className={`text-xs ${active ? 'text-text' : 'text-muted'}`}>{label}</div>
    </div>
  );
}

export default function NextBestSourcePanel({
  confidence, recommendedConnectedCount, recommendedTotal, next, onConnectNext
}: { confidence: Confidence; recommendedConnectedCount: number; recommendedTotal: number; next: Connector | null; onConnectNext: ()=>void }) {
  return (
    <div className="mt-3 rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-semibold text-text">Confidence</div>
      <div className="mt-2 flex items-center justify-between">
        <Step label="Low" active={confidence==='LOW'} />
        <div className="mx-2 h-px flex-1 bg-border" />
        <Step label="Medium" active={confidence==='MEDIUM'} />
        <div className="mx-2 h-px flex-1 bg-border" />
        <Step label="High" active={confidence==='HIGH'} />
      </div>

      <div className="mt-3 text-xs text-muted">
        Connected: <span className="text-text">{recommendedConnectedCount}</span> of <span className="text-text">{recommendedTotal}</span> recommended
      </div>

      <div className="mt-3 rounded-lg border border-border bg-bg/30 p-3">
        {next ? (
          <>
            <div className="text-xs text-muted">Next best source to connect</div>
            <div className="mt-1 text-sm font-semibold text-text">{next.name}</div>
            <div className="mt-1 text-xs text-muted">Adds signals from: <span className="text-text">{next.category}</span></div>
            <div className="mt-3">
              <Button className="w-full" onClick={onConnectNext}>Connect {next.name}</Button>
            </div>
          </>
        ) : (
          <div className="text-sm text-text">All recommended sources connected.</div>
        )}
      </div>
    </div>
  );
}
