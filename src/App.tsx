import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { ConnectorProvider } from './context/ConnectorContext';
import { SourceIntakeProvider } from './context/SourceIntakeContext';
import { DiscoveryRunProvider } from './context/DiscoveryRunContext';
import { PartialResultsProvider } from './context/PartialResultsContext';
import { ToastProvider } from './components/common/Toast';
import IntegrationHubPage from './pages/IntegrationHubPage';
import SourceIntakePage from './pages/SourceIntakePage';
import DiscoveryRunPage from './pages/DiscoveryRunPage';
import PartialResultsPage from './pages/PartialResultsPage';

export default function App() {
  return (
    <ToastProvider>
      <ConnectorProvider>
        <SourceIntakeProvider>
          <DiscoveryRunProvider>
            <PartialResultsProvider>
              <Routes>
                <Route path="/" element={<Navigate to="/integration-hub" replace />} />
                <Route path="/integration-hub" element={<IntegrationHubPage />} />
                <Route path="/source-intake" element={<SourceIntakePage />} />
                <Route path="/discovery-run" element={<DiscoveryRunPage />} />
                <Route path="/partial-results" element={<PartialResultsPage />} />
                <Route path="/reports" element={<div className="min-h-screen text-text p-6">Reports (placeholder)</div>} />
                <Route path="*" element={<Navigate to="/integration-hub" replace />} />
              </Routes>
            </PartialResultsProvider>
          </DiscoveryRunProvider>
        </SourceIntakeProvider>
      </ConnectorProvider>
    </ToastProvider>
  );
}
