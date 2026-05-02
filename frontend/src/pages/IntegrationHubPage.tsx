import React, { useMemo, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import LoadingPanel from '../components/common/LoadingPanel';
import ErrorPanel from '../components/common/ErrorPanel';
import HeroConnectorSection from '../components/integrations/HeroConnectorSection';
import ConnectorGridSection from '../components/integrations/ConnectorGridSection';
import RightPanel from '../components/integrations/RightPanel';
import DiscoveryStartBar from '../components/integrations/DiscoveryStartBar';
import { useToast } from '../components/common/Toast';
import { useConnectorContext } from '../context/ConnectorContext';
import { useRunContext } from '../context/RunContext';
import { useSourceIntakeContext } from '../context/SourceIntakeContext';
import { isDiscoveryReadyConnector } from '../utils/sourceReadiness';

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
    nextBestRecommendedId,
    loading,
    error,
    refetch
  } = useConnectorContext();

  const { push } = useToast();
  const navigate = useNavigate();
  const { runId } = useRunContext();
  const { uploadedFiles, sampleWorkspaceEnabled } = useSourceIntakeContext();
  const [metricAnimation, setMetricAnimation] = useState<{
    connectorId: string;
    key: number;
  } | null>(null);

  const triggerMetricAnimation = (connectorId: string) => {
    setMetricAnimation((current) => ({
      connectorId,
      key: (current?.key ?? 0) + 1,
    }));
  };

  useEffect(() => {
    if (!loading && !selectedConnectorId && recommended && recommended.length > 0) {
      selectConnector(recommended[0].id);
    }
  }, [loading, selectedConnectorId, recommended, selectConnector]);

  useEffect(() => {
    if (loading || !metricAnimation) return;

    const timeoutId = window.setTimeout(() => {
      setMetricAnimation((current) =>
        current?.key === metricAnimation.key ? null : current,
      );
    }, 2200);

    return () => window.clearTimeout(timeoutId);
  }, [loading, metricAnimation]);
  // -------------------------

  const selected = useMemo(
    () => [...recommended, ...standard].find((c) => c.id === selectedConnectorId) ?? null,
    [recommended, standard, selectedConnectorId]
  );

  const next = useMemo(
    () => recommended.find((c) => c.id === nextBestRecommendedId) ?? null,
    [recommended, nextBestRecommendedId]
  );

  const readyConnectorCount = useMemo(
    () => [...recommended, ...standard].filter(isDiscoveryReadyConnector).length,
    [recommended, standard]
  );

  const canStart = readyConnectorCount > 0 || uploadedFiles.length > 0 || sampleWorkspaceEnabled;

  return (
    <div className="min-h-screen text-text">
      <TopNav />

      {loading && <LoadingPanel />}
      {error && !loading && <ErrorPanel message={error} onRetry={refetch} />}

      {!loading && !error && (
        <>
          <div className="w-full px-8 pb-[210px] pt-6 lg:pb-[120px]">
            <div className="mb-6">
              <div className="text-2xl font-semibold">Integration Hub</div>
              <div className="mt-1 text-sm text-muted">The Integration Hub is where users connect enterprise systems to provide data sources for the discovery process.</div>
            </div>

            <div className="flex items-start gap-6">
              <div className="flex flex-[0.7] flex-col gap-6">
                <div className="rounded-xl border border-border bg-panel p-6 shadow-sm">
                  <HeroConnectorSection
                    connectors={recommended}
                    selectedId={selectedConnectorId}
                    onSelect={selectConnector}
                    metricAnimation={metricAnimation}
                    onPrimary={(id) => {
                      const c = recommended.find(x => x.id === id);
                      if (c?.status === 'connected') {
                        triggerMetricAnimation(id);
                        configureSync(id);
                        push('Configuration complete. Data is now synced.');
                      } else {
                        connectConnector(id);
                        push('Connector connected. Click Configure & Sync to load data.');
                      }
                    }}
                    onSecondary={() => push('Data preview available in later Sprint.')}
                  />
                </div>

                <div className="mb-6 rounded-xl border border-border bg-panel p-6 shadow-sm">
                  <ConnectorGridSection
                    connectors={standard}
                    selectedId={selectedConnectorId}
                    onSelect={selectConnector}
                    onPrimary={(id) => {
                      const c = standard.find((x) => x.id === id);
                      if (!c) return;

                      if (c.status === 'connected' && !c.configured) {
                        configureSync(id);
                        push('Configuration complete. Data is now synced.');
                      } else if (c.status === 'connected') {
                        push('Data preview available in later Sprint.');
                      } else if (c.status === 'coming_soon') {
                        push('Connector coming soon.');
                      } else {
                        connectConnector(id);
                        push('Connector connected.');
                      }

                    }}
                  />
                </div>
              </div>

              <div className="flex-[0.3]">
                <RightPanel
                  selected={selected}
                  onConfigure={() => {
                    if (!selected) return;
                    triggerMetricAnimation(selected.id);
                    configureSync(selected.id);
                    push('Configuration complete. Data is now synced.');
                  }}
                  confidence={confidence}
                  recommendedConnectedCount={recommendedConnectedCount}
                  recommendedTotal={3}
                  next={next}
                  onConnectNext={() => {
                    if (!next) return;
                    if (next.status === 'connected') {
                      triggerMetricAnimation(next.id);
                      configureSync(next.id);
                      push('Configuration complete. Data is now synced.');
                    } else {
                      connectConnector(next.id);
                      push('Connected next best source.');
                    }
                  }}
                />
              </div>
            </div>
          </div>

          <DiscoveryStartBar
            confidence={confidence}
            recommendedReadyCount={recommendedConnectedCount}
            recommendedTotal={3}
            recommended={recommended}
            canStart={canStart}
            onStart={() => {
              if (runId) {
                navigate(`/discovery-run?runId=${runId}`);
              } else {
                navigate('/discovery-run', { state: { autoStart: true } });
              }
            }}
            onUpload={() => navigate('/source-intake')}
          />
        </>
      )}
    </div>
  );
}
