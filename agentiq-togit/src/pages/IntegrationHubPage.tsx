import React, { useMemo } from 'react';
import TopNav from '../components/common/TopNav';
import { useConnectorContext } from '../context/ConnectorContext';
import HeroConnectorSection from '../components/integrations/HeroConnectorSection';
import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';
import { useToast } from '../components/common/Toast';

export default function IntegrationHubPage() {
  const {
    connectors,
    recommended,
    selectedConnectorId,
    selectConnector,
    connectConnector,
    configureSync,
  } = useConnectorContext();

  const { push } = useToast();

  const selected = useMemo(
    () => connectors.find((connector) => connector.id === selectedConnectorId) ?? null,
    [connectors, selectedConnectorId],
  );

  return (
    <div className="min-h-screen bg-bg text-text">
      <TopNav />

      <main className="mx-auto max-w-[1440px] px-10 pb-12 pt-6">
        <div className="text-[18px] font-semibold tracking-tight text-text">
          Screen 1 v3: Integration Hub
        </div>

        <div className="mt-5">
          <h1 className="text-[52px] font-semibold leading-none tracking-tight text-text">
            Start here{' '}
            <span className="text-[0.72em] font-normal text-text/90">
              (fastest to value)
            </span>
          </h1>
          <p className="mt-4 text-[18px] text-muted">Connect 1 to start discovery</p>
        </div>

        <div className="mt-6 grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,70%)_minmax(320px,30%)]">
          <div>
            <HeroConnectorSection
              connectors={recommended}
              selectedId={selectedConnectorId}
              onSelect={selectConnector}
              onPrimary={(id) => {
                connectConnector(id);
                push('Connector updated.');
              }}
              onSecondary={() => push('Data preview is a placeholder in this build.')}
            />
          </div>

          <aside className="xl:sticky xl:top-[106px]">
            <ConnectorDetailPanel
              connector={selected}
              onConfigure={() => {
                if (!selected) return;
                configureSync(selected.id);
                push('Sync configured.');
              }}
            />
          </aside>
        </div>
      </main>
    </div>
  );
}