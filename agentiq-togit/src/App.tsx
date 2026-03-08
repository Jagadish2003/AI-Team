import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { ConnectorProvider } from './context/ConnectorContext';
import { ToastProvider } from './components/common/Toast';
import IntegrationHubPage from './pages/IntegrationHubPage';
import SourceIntakePlaceholder from './pages/SourceIntakePlaceholder';

export default function App() {
  return (
    <ToastProvider>
      <ConnectorProvider>
        <Routes>
          <Route path="/" element={<Navigate to="/integration-hub" replace />} />
          <Route path="/integration-hub" element={<IntegrationHubPage />} />
          <Route path="/source-intake" element={<SourceIntakePlaceholder />} />
          <Route path="/reports" element={<div className="min-h-screen text-text p-6">Reports (placeholder)</div>} />
          <Route path="*" element={<Navigate to="/integration-hub" replace />} />
        </Routes>
      </ConnectorProvider>
    </ToastProvider>
  );
}
