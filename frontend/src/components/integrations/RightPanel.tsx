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
    <div className="sticky top-[76px] flex flex-col gap-3">
      <ConnectorDetailPanel connector={selected} onConfigure={onConfigure} />
      {/* T41-8: File upload config merged from SourceIntakePage into right panel.
          Collapsible so it does not dominate the connector detail view. */}
      <SourceConfigPanel />
      <NextBestSourcePanel confidence={confidence} recommendedConnectedCount={recommendedConnectedCount} recommendedTotal={recommendedTotal} next={next} onConnectNext={onConnectNext} />
    </div>
  );
}
