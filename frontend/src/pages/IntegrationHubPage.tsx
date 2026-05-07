import React, { useMemo, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import PageShell from "../components/common/PageShell";
import LoadingPanel from "../components/common/LoadingPanel";
import ErrorPanel from "../components/common/ErrorPanel";
import HeroConnectorSection from "../components/integrations/HeroConnectorSection";
import ConnectorGridSection from "../components/integrations/ConnectorGridSection";
import RightPanel from "../components/integrations/RightPanel";
import DiscoveryStartBar from "../components/integrations/DiscoveryStartBar";
import { useToast } from "../components/common/Toast";
import { useConnectorContext } from "../context/ConnectorContext";
import { useRunContext } from "../context/RunContext";
import { useSourceIntakeContext } from "../context/SourceIntakeContext";
import { isDiscoveryReadyConnector } from "../utils/sourceReadiness";

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
    refetch,
  } = useConnectorContext();

  const { push } = useToast();
  const navigate = useNavigate();
  const { runId } = useRunContext();
  // T41-8: sampleWorkspaceEnabled no longer imported — not used in canStart.
  const { uploadedFiles } = useSourceIntakeContext();
  const [metricAnimation, setMetricAnimation] = useState<{
    connectorId: string;
    key: number;
  } | null>(null);

  useEffect(() => {
    if (
      !loading &&
      !selectedConnectorId &&
      recommended &&
      recommended.length > 0
    ) {
      selectConnector(recommended[0].id);
    }
  }, [loading, selectedConnectorId, recommended, selectConnector]);
  // -------------------------

  const selected = useMemo(
    () =>
      [...recommended, ...standard].find((c) => c.id === selectedConnectorId) ??
      null,
    [recommended, standard, selectedConnectorId],
  );

  const next = useMemo(
    () => recommended.find((c) => c.id === nextBestRecommendedId) ?? null,
    [recommended, nextBestRecommendedId],
  );

  const readyConnectorCount = useMemo(
    () =>
      [...recommended, ...standard].filter(isDiscoveryReadyConnector).length,
    [recommended, standard],
  );

  // T41-8: canStart no longer reads sampleWorkspaceEnabled.
  // The Sample Workspace demo pathway is removed from runtime logic.
  // Offline mode for engineering stays behind INGEST_MODE=offline env var.
  const canStart = readyConnectorCount > 0 || uploadedFiles.length > 0;

  const syncConnector = (id: string) => {
    configureSync(id);
    if (recommended.some((connector) => connector.id === id)) {
      setMetricAnimation((prev) => ({
        connectorId: id,
        key: (prev?.key ?? 0) + 1,
      }));
    }
  };

  useEffect(() => {
    if (!metricAnimation || loading) return;

    const timeoutId = window.setTimeout(() => {
      setMetricAnimation(null);
    }, 2200);

    return () => window.clearTimeout(timeoutId);
  }, [loading, metricAnimation]);

  return (
    <>
      <PageShell
        title="Integration Hub"
        description="Connect enterprise systems and optional file sources to provide data for discovery."
        contentClassName="pb-[190px] xl:pb-28"
      >
        {loading && <LoadingPanel />}
        {error && !loading && <ErrorPanel message={error} onRetry={refetch} />}

        {!loading && !error && (
            <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
              <div className="flex min-w-0 flex-col gap-6">
                <div className="rounded-xl border border-border bg-panel p-6 shadow-sm">
                  <HeroConnectorSection
                    connectors={recommended}
                    selectedId={selectedConnectorId}
                    onSelect={selectConnector}
                    onPrimary={(id) => {
                      const c = recommended.find((x) => x.id === id);
                      if (c?.status === "connected") {
                        syncConnector(id);
                        push("Configuration complete. Data is now synced.");
                      } else {
                        setMetricAnimation(null);
                        connectConnector(id);
                        push(
                          "Connector connected. Click Configure & Sync to load data.",
                        );
                      }
                    }}
                    onSecondary={() =>
                      push("Data preview available in later Sprint.")
                    }
                    metricAnimation={metricAnimation}
                  />
                </div>

                <div className="rounded-xl border border-border bg-panel p-6 shadow-sm">
                  <ConnectorGridSection
                    connectors={standard}
                    selectedId={selectedConnectorId}
                    onSelect={selectConnector}
                    onPrimary={(id) => {
                      const c = standard.find((x) => x.id === id);
                      if (!c) return;

                      if (c.status === "connected" && !c.configured) {
                        syncConnector(id);
                        push("Configuration complete. Data is now synced.");
                      } else if (c.status === "connected") {
                        push("Data preview available in later Sprint.");
                      } else if (c.status === "coming_soon") {
                        push("Connector coming soon.");
                      } else {
                        setMetricAnimation(null);
                        connectConnector(id);
                        push("Connector connected.");
                      }
                    }}
                  />
                </div>
              </div>

              <div className="min-w-0">
                <RightPanel
                  selected={selected}
                  onConfigure={() => {
                    if (!selected) return;
                    syncConnector(selected.id);
                    push("Configuration complete. Data is now synced.");
                  }}
                  confidence={confidence}
                  recommendedConnectedCount={recommendedConnectedCount}
                  recommendedTotal={3}
                  next={next}
                  onConnectNext={() => {
                    if (!next) return;
                    if (next.status === "connected") {
                      syncConnector(next.id);
                      push("Configuration complete. Data is now synced.");
                    } else {
                      setMetricAnimation(null);
                      connectConnector(next.id);
                      push("Connected next best source.");
                    }
                  }}
                />
              </div>
            </div>
        )}
      </PageShell>
      {!loading && !error && (
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
                navigate("/discovery-run", { state: { autoStart: true } });
              }
            }}
          />
      )}
    </>
  );
}
