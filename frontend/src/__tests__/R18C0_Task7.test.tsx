/**
 * R18-C0 / P7 — connector-aware Blueprint naming.
 *
 * Salesforce branding is presentation-only: an actively connected Salesforce
 * org sees "Agentforce Blueprint" while every other connector state keeps the
 * neutral "Agent Blueprint" label and the same route.
 */
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  AGENT_BLUEPRINT_LABEL,
  AGENTFORCE_BLUEPRINT_LABEL,
  getBlueprintLabel,
  isSalesforceConnected,
} from '../utils/blueprintNaming';

const state = vi.hoisted(() => ({
  connectors: [] as Array<{ id: string; status: string }>,
  runId: null as string | null,
}));

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: state.runId }),
}));

vi.mock('../context/ConnectorContext', () => ({
  useConnectorContext: () => ({ all: state.connectors }),
}));

vi.mock('../context/LicenseContext', () => ({
  useOrgName: () => 'Task 7 Org',
}));

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({
    opportunities: [],
    selectedId: null,
    select: vi.fn(),
  }),
}));

vi.mock('../context/DiscoveryRunContext', () => ({
  useDiscoveryRunContext: () => ({ run: null, computing: false }),
}));

vi.mock('../components/common/PageShell', () => ({
  default: ({ title, description, children }: any) => (
    <main>
      <h1>{title}</h1>
      <p>{description}</p>
      {children}
    </main>
  ),
}));

import TopNav from '../components/common/TopNav';
import BlueprintPage from '../pages/BlueprintPage';

function renderTopNav(connectors: Array<{ id: string; status: string }>) {
  state.connectors = connectors;
  state.runId = 'run_task7';
  return render(
    <MemoryRouter initialEntries={['/agentforce-blueprint']}>
      <TopNav />
    </MemoryRouter>,
  );
}

function renderBlueprintPage(connectors: Array<{ id: string; status: string }>) {
  state.connectors = connectors;
  state.runId = null;
  return render(
    <MemoryRouter initialEntries={['/agentforce-blueprint']}>
      <BlueprintPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  state.connectors = [];
  state.runId = null;
});

describe('R18-C0 P7 — Blueprint naming contract', () => {
  it('uses Agentforce Blueprint only for a connected Salesforce org', () => {
    const connectors = [{ id: 'salesforce', status: 'connected' }];

    expect(isSalesforceConnected(connectors)).toBe(true);
    expect(getBlueprintLabel(isSalesforceConnected(connectors))).toBe(
      AGENTFORCE_BLUEPRINT_LABEL,
    );

    renderTopNav(connectors);
    const link = screen.getByRole('link', { name: AGENTFORCE_BLUEPRINT_LABEL });
    expect(link).toHaveAttribute('href', '/agentforce-blueprint?runId=run_task7');
    expect(screen.queryByText(AGENT_BLUEPRINT_LABEL)).not.toBeInTheDocument();
  });

  it.each([
    ['no connector record', []],
    ['disconnected Salesforce', [{ id: 'salesforce', status: 'disconnected' }]],
    ['not-connected Salesforce', [{ id: 'salesforce', status: 'not_connected' }]],
    ['connected non-Salesforce source', [{ id: 'servicenow', status: 'connected' }]],
  ])('uses Agent Blueprint for %s', (_caseName, connectors) => {
    expect(isSalesforceConnected(connectors)).toBe(false);
    expect(getBlueprintLabel(isSalesforceConnected(connectors))).toBe(
      AGENT_BLUEPRINT_LABEL,
    );

    renderTopNav(connectors);
    const label = screen.getByText(AGENT_BLUEPRINT_LABEL);
    const link = label.closest('a');
    expect(link).toHaveAttribute('href', '/agentforce-blueprint?runId=run_task7');
    expect(screen.queryByText(AGENTFORCE_BLUEPRINT_LABEL)).not.toBeInTheDocument();
  });

  it('presents the Blueprint page itself with Agentforce naming for connected Salesforce', () => {
    renderBlueprintPage([{ id: 'salesforce', status: 'connected' }]);

    expect(
      screen.getByRole('heading', { name: AGENTFORCE_BLUEPRINT_LABEL }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/inspect the Agentforce Blueprint for the selected opportunity/i),
    ).toBeInTheDocument();
  });

  it('keeps the Blueprint page itself generic without connected Salesforce', () => {
    renderBlueprintPage([{ id: 'salesforce', status: 'disconnected' }]);

    expect(
      screen.getByRole('heading', { name: AGENT_BLUEPRINT_LABEL }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/inspect the Agent Blueprint for the selected opportunity/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('heading', { name: AGENTFORCE_BLUEPRINT_LABEL }),
    ).not.toBeInTheDocument();
  });
});
