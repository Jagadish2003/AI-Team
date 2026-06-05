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
import { ThemeProvider } from "./context/ThemeContext";

import IntegrationHubPage from "./pages/IntegrationHubPage";
import DiscoveryRunPage from "./pages/DiscoveryRunPage";
import OpportunityReviewPage from "./pages/OpportunityReviewPage";
import SourceIntelligencePage from "./pages/SourceIntelligencePage";
import PilotRoadmapPage from "./pages/PilotRoadmapPage";
import BlueprintPage from "./pages/BlueprintPage";
import ExecutiveReportPage from "./pages/ExecutiveReportPage";
import StackBuilderPage from "./pages/StackBuilderPage";

export default function App() {
  return (
    <ThemeProvider>
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
                          path="/stack-builder"
                          element={<StackBuilderPage />}
                        />
                        <Route
                          path="/discovery-run"
                          element={<DiscoveryRunPage />}
                        />
                        {/* Evidence Collection hidden — Sprint 5.1
                        <Route
                          path="/partial-results"
                          element={<PartialResultsPage />}
                        /> */}
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
                          path="/agent-blueprint"
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
    </ThemeProvider>
  );
}
