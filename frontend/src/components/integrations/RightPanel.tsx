import React from 'react';
import { Connector } from '../../types/connector';
import { Confidence } from '../../utils/confidence';
import ConnectorDetailPanel from './ConnectorDetailPanel';
import NextBestSourcePanel from './NextBestSourcePanel';

export default function RightPanel({
  selected, onConfigure, confidence, recommendedConnectedCount, recommendedTotal, next, onConnectNext
}: { selected: Connector | null; onConfigure: ()=>void; confidence: Confidence; recommendedConnectedCount: number; recommendedTotal: number; next: Connector | null; onConnectNext: ()=>void }) {
  return (
    <div className="sticky top-[76px]">
      <ConnectorDetailPanel connector={selected} onConfigure={onConfigure} />
      <NextBestSourcePanel confidence={confidence} recommendedConnectedCount={recommendedConnectedCount} recommendedTotal={recommendedTotal} next={next} onConnectNext={onConnectNext} />
    </div>
  );
}
