/**
 * IntegrationHub cloud-connector tile registration — MSP-B13 (AT-748, T6).
 *
 * Covers T6-AC1 / AC4: the AWS and Azure Event tiles are registered from the
 * connector CATALOG (the context, fed by GET /api/connectors), grouped under
 * "Cloud Operations" by the catalog's `multiScope` attribute — never a hardcoded
 * id list. The group's membership tracks exactly what the catalog returns.
 *
 * Run: npx vitest run src/__tests__/IntegrationHubCloudTiles.test.tsx
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../services/staticApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/staticApi')>();
  return { ...actual, fetchTokenStatus: vi.fn().mockResolvedValue({ status: 'connected' }) };
});
vi.mock('../context/ConnectorContext', () => ({ useConnectorContext: vi.fn() }));
vi.mock('../components/common/Toast', () => ({ useToast: vi.fn() }));
vi.mock('../api/licenseApi', () => ({ fetchLicenseLimits: vi.fn() }));
vi.mock('../components/common/PageShell', () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock('../components/integrations/RightPanel', () => ({ default: () => <div /> }));

import IntegrationHubPage from '../pages/IntegrationHubPage';
import { useConnectorContext } from '../context/ConnectorContext';
import { useToast } from '../components/common/Toast';
import { fetchLicenseLimits } from '../api/licenseApi';

function awsEvents(over: Record<string, unknown> = {}) {
  return {
    id: 'aws_events',
    name: 'AWS Events',
    category: 'Cloud Operations · Multi-account',
    tier: 'standard' as const,
    status: 'not_configured' as const,
    configured: false,
    multiScope: true,
    scopeNoun: 'account',
    metrics: [],
    lastSynced: '—',
    reads: ['CloudWatch Alarms'],
    signalStrength: 55,
    ...over,
  };
}

function azureEvents(over: Record<string, unknown> = {}) {
  return {
    id: 'azure_events',
    name: 'Azure Events',
    category: 'Cloud Operations · Multi-subscription',
    tier: 'standard' as const,
    status: 'not_configured' as const,
    configured: false,
    multiScope: true,
    scopeNoun: 'subscription',
    metrics: [],
    lastSynced: '—',
    reads: ['Monitor Alerts'],
    signalStrength: 55,
    ...over,
  };
}

function mockContext(standard: Record<string, unknown>[]) {
  vi.mocked(useConnectorContext).mockReturnValue({
    recommended: [],
    standard,
    selectedConnectorId: null,
    selectConnector: vi.fn(),
    connectConnector: vi.fn(),
    configureSync: vi.fn(),
    disconnectConnector: vi.fn(),
    loading: false,
    error: null,
    refetch: vi.fn(),
  } as any);
}

function renderHub() {
  render(
    <MemoryRouter initialEntries={['/integration-hub']}>
      <IntegrationHubPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useToast).mockReturnValue({ push: vi.fn() } as any);
  vi.mocked(fetchLicenseLimits).mockResolvedValue({
    systemsUsed: 0,
    systemsLicensed: 10,
    unlimited: false,
    canConnectMore: true,
  } as any);
});

describe('Integration Hub — catalog-driven cloud tiles (T6-AC1/AC4)', () => {
  it('registers AWS + Azure tiles from the catalog under Cloud Operations', async () => {
    mockContext([awsEvents(), azureEvents()]);
    renderHub();
    expect(await screen.findByText('Cloud Operations')).toBeInTheDocument();
    expect(screen.getByText('AWS Events')).toBeInTheDocument();
    expect(screen.getByText('Azure Events')).toBeInTheDocument();
    // Not-connected multi-scope tiles offer a "Set up" action (opens the panel),
    // not the OAuth "Connect" flow they do not use.
    expect(screen.getAllByRole('button', { name: /set up/i }).length).toBeGreaterThanOrEqual(2);
  });

  it('tracks the catalog exactly — a connector the catalog omits is not registered', async () => {
    // Catalog returns ONLY aws_events → azure_events must not appear. Proves the
    // group is not built from a hardcoded id list (T6-AC4).
    mockContext([awsEvents()]);
    renderHub();
    expect(await screen.findByText('AWS Events')).toBeInTheDocument();
    expect(screen.queryByText('Azure Events')).not.toBeInTheDocument();
  });

  it('shows the group empty state when the catalog has no multi-scope connectors', async () => {
    mockContext([]);
    renderHub();
    // The Cloud Operations group still exists (from group metadata) but registers
    // no tiles when the catalog flags none multiScope.
    expect(await screen.findByText('Cloud Operations')).toBeInTheDocument();
    expect(screen.queryByText('AWS Events')).not.toBeInTheDocument();
  });
});
