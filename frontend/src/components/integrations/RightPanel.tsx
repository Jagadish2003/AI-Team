import React from 'react';
import { Connector } from '../../types/connector';
import { Confidence } from '../../utils/confidence';
import ConnectorDetailPanel from './ConnectorDetailPanel';
import NextBestSourcePanel from './NextBestSourcePanel';
import SourceConfigPanel from './SourceConfigPanel';

export default function RightPanel({
  selected, onConfigure, confidence, recommendedConnectedCount, recommendedTotal, next, onConnectNext
}: { selected: Connector | null; onConfigure: ()=>void; confidence: Confidence; recommendedConnectedCount: number; recommendedTotal: number; next: Connector | null; onConnectNext: ()=>void }) {
  return (
    <div className="flex min-w-0 flex-col gap-6 xl:contents">
      <ConnectorDetailPanel
        connector={selected}
        onConfigure={onConfigure}
        className="xl:col-start-2 xl:row-start-1 xl:h-full"
      />
      <div className="flex min-w-0 flex-col gap-6 xl:col-start-2 xl:row-start-2">
        {/* T41-8: File upload config merged from SourceIntakePage into right panel.
            Collapsible so it does not dominate the connector detail view. */}
        <SourceConfigPanel />
        <NextBestSourcePanel confidence={confidence} recommendedConnectedCount={recommendedConnectedCount} recommendedTotal={recommendedTotal} next={next} onConnectNext={onConnectNext} />
      </div>
    </div>
  );
}
