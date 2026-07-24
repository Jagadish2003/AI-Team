/**
 * The static-credential "Connection credentials" section (StaticCredentialManager)
 * is a NO-PUBLIC-INBOUND feature: its only write entry point is the "Set up
 * outbound access" button, which exists only when noPublicInbound. So
 * ConnectorDetailPanel must render the credentials section ONLY in a
 * no_public_inbound deployment — in the standard profile ServiceNow / Jira
 * authenticate via the normal Connect (OAuth) flow and the card would just point
 * at a button that isn't there.
 */
import '@testing-library/jest-dom/vitest';
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Controllable network-profile mock.
const np = { hide: false };
vi.mock('../context/NetworkProfileContext', () => ({
  useNetworkProfileOptional: () => ({
    noPublicInbound: np.hide,
    capabilities: {},
    capabilityFor: () => null,
    hidesAuthorizationCodeConnect: () => false,
    loading: false,
  }),
}));
vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({ user: { email: 'owner@oilcorp.com', role: 'owner' } }),
}));
vi.mock('../components/common/Toast', () => ({ useToast: () => ({ push: vi.fn() }) }));
// Stub the heavy children so we test the GATING decision, not their internals.
vi.mock('../components/integrations/StaticCredentialManager', () => ({
  default: () => <div data-testid="connection-credentials">Connection credentials</div>,
}));
vi.mock('../components/integrations/OutboundAuthSetup', () => ({ default: () => null }));

import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';

function servicenow() {
  return {
    id: 'servicenow',
    name: 'ServiceNow',
    category: 'Operations · Incidents',
    tier: 'recommended' as const,
    recommendedRank: 2,
    status: 'connected' as const,
    configured: false,
    metrics: [],
    lastSynced: 'Just now',
    reads: ['Incident Tickets', 'Change Requests', 'SLA Definitions'],
    signalStrength: 88,
  };
}

describe('ConnectorDetailPanel — credentials section gated on network profile', () => {
  beforeEach(() => {
    np.hide = false;
  });

  it('standard profile: hides the "Connection credentials" section for ServiceNow', () => {
    np.hide = false;
    render(<ConnectorDetailPanel connector={servicenow() as any} onConfigure={vi.fn()} />);
    expect(screen.queryByTestId('connection-credentials')).toBeNull();
  });

  it('no_public_inbound: shows the "Connection credentials" section for ServiceNow', () => {
    np.hide = true;
    render(<ConnectorDetailPanel connector={servicenow() as any} onConfigure={vi.fn()} />);
    expect(screen.getByTestId('connection-credentials')).toBeInTheDocument();
  });

  it('standard profile: also hides it for Jira', () => {
    np.hide = false;
    render(
      <ConnectorDetailPanel
        connector={{ ...servicenow(), id: 'jira', name: 'Jira' } as any}
        onConfigure={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('connection-credentials')).toBeNull();
  });
});
