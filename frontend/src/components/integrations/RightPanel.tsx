import React from 'react';
import { Connector, OutboundSetupRequest } from '../../types/connector';
import { Confidence } from '../../utils/confidence';
import ConnectorDetailPanel from './ConnectorDetailPanel';
import NextBestSourcePanel from './NextBestSourcePanel';
import SourceConfigPanel from './SourceConfigPanel';

export default function RightPanel({
  selected, onConfigure, confidence, recommendedConnectedCount, recommendedTotal, next, onConnectNext,
  outboundSetupRequest = null,
}: { selected: Connector | null; onConfigure: ()=>void; confidence: Confidence; recommendedConnectedCount: number; recommendedTotal: number; next: Connector | null; onConnectNext: ()=>void; outboundSetupRequest?: OutboundSetupRequest | null }) {
  return (
    <div className="sticky top-[76px] flex flex-col gap-3">
      {/* key on connector id: switching connectors remounts the detail panel so
          each provider gets an isolated, fresh validation/error/form state. This
          prevents one connector's error (e.g. an AWS AssumeRole failure) from
          leaking into another (e.g. Azure), and applies uniformly to every
          connector — AWS, Azure, ServiceNow, Jira, and future providers. */}
      <ConnectorDetailPanel key={selected?.id ?? 'none'} connector={selected} onConfigure={onConfigure} outboundSetupRequest={outboundSetupRequest} />
      {/* T41-8: File upload config merged from SourceIntakePage into right panel.
          Collapsible so it does not dominate the connector detail view. */}
      <SourceConfigPanel />
      <NextBestSourcePanel confidence={confidence} recommendedConnectedCount={recommendedConnectedCount} recommendedTotal={recommendedTotal} next={next} onConnectNext={onConnectNext} />
    </div>
  );
}
