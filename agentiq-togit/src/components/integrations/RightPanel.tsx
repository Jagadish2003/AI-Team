import React from 'react';
import { Connector } from '../../types/connector';
import { Confidence } from '../../utils/confidence';
import ConnectorDetailPanel from './ConnectorDetailPanel';
import NextBestSourcePanel from './NextBestSourcePanel';

export default function RightPanel({
  selected,
  onConfigure,
  confidence,
  recommendedConnectedCount,
  recommendedTotal,
  next,
  onConnectNext,
}: {
  selected: Connector | null;
  onConfigure: () => void;
  confidence: Confidence;
  recommendedConnectedCount: number;
  recommendedTotal: number;
  next: Connector | null;
  onConnectNext: () => void;
}) {
  return (
    <aside className="sticky top-[106px] flex flex-col gap-4">
      <ConnectorDetailPanel connector={selected} onConfigure={onConfigure} />
      <NextBestSourcePanel
        confidence={confidence}
        recommendedConnectedCount={recommendedConnectedCount}
        recommendedTotal={recommendedTotal}
        next={next}
        onConnectNext={onConnectNext}
      />
    </aside>
  );
}
