import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { ConnectorProvider } from './context/ConnectorContext';
import { SourceIntakeProvider } from './context/SourceIntakeContext';
import { RunProvider } from './context/RunContext'; 
import { DiscoveryRunProvider } from './context/DiscoveryRunContext';
import { PartialResultsProvider } from './context/PartialResultsContext';
import { NormalizationProvider } from './context/NormalizationContext';
import { ToastProvider } from './components/common/Toast';
import { AnalystReviewProvider } from './context/AnalystReviewContext';

import IntegrationHubPage from './pages/IntegrationHubPage';
import SourceIntakePage from './pages/SourceIntakePage';
import DiscoveryRunPage from './pages/DiscoveryRunPage';
import PartialResultsPage from './pages/PartialResultsPage';
import NormalizationInspectorPage from './pages/NormalizationInspectorPage';
import AnalystReviewPage from './pages/AnalystReviewPage';
import OpportunityMapPage from './pages/OpportunityMapPage';
import PilotRoadmapPage from './pages/PilotRoadmapPage';
import ExecutiveReportPage from './pages/ExecutiveReportPage';

export default function App() {
  return (
    <ToastProvider>
      <ConnectorProvider>
        <SourceIntakeProvider>
          <RunProvider>
            <DiscoveryRunProvider>
              <PartialResultsProvider>
                <NormalizationProvider>
                  <AnalystReviewProvider>
                  <Routes>
                    <Route path="/" element={<Navigate to="/integration-hub" replace />} />
                    <Route path="/integration-hub" element={<IntegrationHubPage />} />
                    <Route path="/source-intake" element={<SourceIntakePage />} />
                    <Route path="/discovery-run" element={<DiscoveryRunPage />} />
                    <Route path="/partial-results" element={<PartialResultsPage />} />
                    <Route path="/normalization" element={<NormalizationInspectorPage />} />
                    <Route path="/analyst-review" element={<AnalystReviewPage />} />
                    <Route path="/opportunity-map" element={<OpportunityMapPage />} />
                    <Route path="/pilot-roadmap" element={<PilotRoadmapPage />} />
                    <Route path="/executive-report" element={<ExecutiveReportPage />} />
                    <Route path="*" element={<Navigate to="/integration-hub" replace />} />
                  </Routes>
                  </AnalystReviewProvider>
                </NormalizationProvider>
              </PartialResultsProvider>
            </DiscoveryRunProvider>
          </RunProvider>
        </SourceIntakeProvider>
      </ConnectorProvider>
    </ToastProvider>
  );
}
