import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { ConnectorProvider } from './context/ConnectorContext';
import { SourceIntakeProvider } from './context/SourceIntakeContext';
import { ToastProvider } from './components/common/Toast';
import IntegrationHubPage from './pages/IntegrationHubPage';
import SourceIntakePage from './pages/SourceIntakePage';

export default function App() {
  return (
    <ToastProvider>
      <ConnectorProvider>
        <SourceIntakeProvider>
          <Routes>
            <Route path="/" element={<Navigate to="/integration-hub" replace />} />
            <Route path="/integration-hub" element={<IntegrationHubPage />} />
            <Route path="/source-intake" element={<SourceIntakePage />} />
            <Route path="/reports" element={<div className="min-h-screen text-text p-6">Reports (placeholder)</div>} />
            <Route path="*" element={<Navigate to="/integration-hub" replace />} />
          </Routes>
        </SourceIntakeProvider>
      </ConnectorProvider>
    </ToastProvider>
  );
}
