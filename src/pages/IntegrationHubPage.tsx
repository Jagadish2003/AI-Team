import React, { useMemo } from 'react';
import TopNav from '../components/common/TopNav';
import { useConnectorContext } from '../context/ConnectorContext';
import HeroConnectorSection from '../components/integrations/HeroConnectorSection';
import ConnectorGridSection from '../components/integrations/ConnectorGridSection';
import RightPanel from '../components/integrations/RightPanel';
import DiscoveryStartBar from '../components/integrations/DiscoveryStartBar';
import { useToast } from '../components/common/Toast';
import { useNavigate } from 'react-router-dom';

export default function IntegrationHubPage() {

  const {
    recommended,
    standard,
    selectedConnectorId,
    selectConnector,
    connectConnector,
    configureSync,
    confidence,
    recommendedConnectedCount,
    nextBestRecommendedId
  } = useConnectorContext();

  const { push } = useToast();
  const navigate = useNavigate();

  const selected = useMemo(
    () => [...recommended, ...standard].find(c => c.id === selectedConnectorId) ?? null,
    [recommended, standard, selectedConnectorId]
  );

  const next = useMemo(
    () => recommended.find(c => c.id === nextBestRecommendedId) ?? null,
    [recommended, nextBestRecommendedId]
  );

  const canStart = recommendedConnectedCount >= 1;

  return (
    <div className="min-h-screen text-text">

      {/* TOP NAV */}
      <TopNav />

      {/* PAGE CONTAINER */}
      <div className="w-full px-8 pt-6 pb-[210px] lg:pb-[120px]">

        {/* PAGE TITLE */}
        <div className="mb-6">
          <div className="text-2xl font-semibold">
            Integration Hub
          </div>

          <div className="mt-1 text-sm text-muted">
            Connect at least 1 source to start discovery.
          </div>
        </div>

        {/* MAIN LAYOUT */}
        <div className="flex items-start gap-6">

          {/* LEFT SIDE (70%) */}
          <div className="flex-[0.7] flex flex-col gap-6">

            {/* RECOMMENDED CONNECTORS */}
            <div className="rounded-xl border border-border bg-panel p-6 shadow-sm">

              <HeroConnectorSection
                connectors={recommended}
                selectedId={selectedConnectorId}
                onSelect={selectConnector}
                onPrimary={(id) => {
                  connectConnector(id);
                  push('Connector connected (mock).');
                }}
                onSecondary={() =>
                  push('Data preview available in Sprint 2.')
                }
              />

            </div>

            {/* STANDARD CONNECTORS */}
            <div className="rounded-xl border border-border bg-panel p-6 shadow-sm mb-6">

              <ConnectorGridSection
                connectors={standard.filter(c => c.status !== "coming_soon")}
                selectedId={selectedConnectorId}
                onSelect={selectConnector}
                onPrimary={(id) => {

                  const c = standard.find(x => x.id === id);
                  if (!c) return;

                  if (c.status === 'connected') {
                    push('Data preview available in Sprint 2.');
                  }
                  else {
                    connectConnector(id);
                    push('Connector connected (mock).');
                  }

                }}
              />

            </div>

          </div>

          {/* RIGHT PANEL (30%) */}
          <div className="flex-[0.3]">

            <RightPanel
              selected={selected}
              onConfigure={() => {

                if (!selected) return;

                configureSync(selected.id);

                push('Sync configured (mock).');

              }}
              confidence={confidence}
              recommendedConnectedCount={recommendedConnectedCount}
              recommendedTotal={3}
              next={next}
              onConnectNext={() => {

                if (!next) return;

                connectConnector(next.id);

                push('Connected next best source (mock).');

              }}
            />

          </div>

        </div>

      </div>

      {/* STICKY DISCOVERY BAR */}
      <DiscoveryStartBar
        confidence={confidence}
        recommendedConnectedCount={recommendedConnectedCount}
        recommendedTotal={3}
        canStart={canStart}
        onStart={() => push('Discovery run started (mock).')}
        onUpload={() => navigate('/source-intake')}
      />

    </div>
  );
}