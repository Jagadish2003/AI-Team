import React from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { ConnectorProvider } from "./context/ConnectorContext";
import { SourceIntakeProvider } from "./context/SourceIntakeContext";
import { RunProvider } from "./context/RunContext";
import { DiscoveryRunProvider } from "./context/DiscoveryRunContext";
import { PartialResultsProvider } from "./context/PartialResultsContext";
import { NormalizationProvider } from "./context/NormalizationContext";
import { ToastProvider } from "./components/common/Toast";
import { AnalystReviewProvider } from "./context/AnalystReviewContext";
import { EvidenceProvider } from "./context/EvidenceContext";

import IntegrationHubPage from "./pages/IntegrationHubPage";
import SourceIntakePage from "./pages/SourceIntakePage";
import DiscoveryRunPage from "./pages/DiscoveryRunPage";
import PartialResultsPage from "./pages/PartialResultsPage";
import NormalizationInspectorPage from "./pages/NormalizationInspectorPage";
import AnalystReviewPage from "./pages/AnalystReviewPage";
import OpportunityMapPage from "./pages/OpportunityMapPage";
import OpportunityReviewPage from "./pages/OpportunityReviewPage"; // T41-2
import SourceIntelligencePage from "./pages/SourceIntelligencePage";
import PilotRoadmapPage from "./pages/PilotRoadmapPage";
import BlueprintPage from "./pages/BlueprintPage";
import ExecutiveReportPage from "./pages/ExecutiveReportPage";

export default function App() {
  return (
    <ToastProvider>
      <ConnectorProvider>
        <RunProvider>
          <SourceIntakeProvider>
            <DiscoveryRunProvider>
              <PartialResultsProvider>
                <NormalizationProvider>
                  <AnalystReviewProvider>
                    <EvidenceProvider>
                      <Routes>
                        <Route
                          path="/"
                          element={<Navigate to="/integration-hub" replace />}
                        />
                        <Route
                          path="/integration-hub"
                          element={<IntegrationHubPage />}
                        />
                        <Route
                          path="/source-intake"
                          // T41-8: Source Intake merged into Integration Hub.
                          // Redirect preserved for backward compatibility.
                          element={<Navigate to="/integration-hub" replace />}
                        />
                        <Route
                          path="/discovery-run"
                          element={<DiscoveryRunPage />}
                        />
                        <Route
                          path="/partial-results"
                          element={<PartialResultsPage />}
                        />
                        <Route
                          path="/normalization"
                          element={
                            <Navigate to="/source-intelligence" replace />
                          }
                        />
                        <Route
                          path="/source-intelligence"
                          element={<SourceIntelligencePage />}
                        />
                        <Route
                          path="/analyst-review"
                          element={
                            <Navigate to="/opportunity-review" replace />
                          }
                        />

                        <Route
                          path="/opportunity-map"
                          element={
                            <Navigate to="/opportunity-review" replace />
                          }
                        />
                        <Route
                          path="/opportunity-review"
                          element={<OpportunityReviewPage />}
                        />
                        <Route
                          path="/pilot-roadmap"
                          element={<PilotRoadmapPage />}
                        />
                        <Route
                          path="/agentforce-blueprint"
                          element={<BlueprintPage />}
                        />
                        <Route
                          path="/executive-report"
                          element={<ExecutiveReportPage />}
                        />
                        <Route
                          path="*"
                          element={<Navigate to="/integration-hub" replace />}
                        />
                      </Routes>
                    </EvidenceProvider>
                  </AnalystReviewProvider>
                </NormalizationProvider>
              </PartialResultsProvider>
            </DiscoveryRunProvider>
          </SourceIntakeProvider>
        </RunProvider>
      </ConnectorProvider>
    </ToastProvider>
  );
}
