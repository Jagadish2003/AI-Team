/**
 * R18-A3 T5 (AT-558) — ConnectorTile behaviour under NETWORK_PROFILE.
 *
 * In a no-public-inbound deployment, a connector with an outbound-only mode must
 * NOT offer the browser authorization-code Connect flow — the tile shows "Set up
 * outbound access" and clicking it opens the detail panel (onSelect) instead of
 * starting OAuth (onPrimary). This is the AC4 guarantee: the customer can never
 * start a flow that cannot complete.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

import ConnectorTile from '../components/integrations/ConnectorTile';

vi.mock('../services/staticApi', () => ({
  fetchTokenStatus: vi.fn().mockResolvedValue({ status: 'connected' }),
}));

vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({ user: { email: 'owner@dwp.com', role: 'owner' } }),
}));

// Controllable network-profile mock.
const np: { hide: boolean } = { hide: false };
vi.mock('../context/NetworkProfileContext', () => ({
  useNetworkProfileOptional: () => ({
    noPublicInbound: np.hide,
    capabilities: {},
    capabilityFor: () => null,
    hidesAuthorizationCodeConnect: (id: string) =>
      np.hide && id === 'salesforce',
    loading: false,
  }),
}));

function disconnectedSalesforce() {
  return {
    id: 'salesforce',
    name: 'Salesforce',
    category: 'CRM',
    tier: 'recommended' as const,
    recommendedRank: 1,
    status: 'disconnected' as const,
    configured: false,
    metrics: [],
    lastSynced: '',
    reads: ['Accounts'],
    signalStrength: 90,
  };
}

function renderTile(handlers: {
  onSelect?: () => void;
  onPrimary?: () => void;
  onSetupOutbound?: () => void;
}) {
  const onSelect = handlers.onSelect ?? vi.fn();
  const onPrimary = handlers.onPrimary ?? vi.fn();
  const onSetupOutbound = handlers.onSetupOutbound;
  render(
    <ConnectorTile
      connector={disconnectedSalesforce() as any}
      icon={<span>ic</span>}
      selected={false}
      onSelect={onSelect}
      onPrimary={onPrimary}
      onReconnect={vi.fn()}
      onSetupOutbound={onSetupOutbound}
    />,
  );
  return { onSelect, onPrimary, onSetupOutbound };
}

beforeEach(() => {
  vi.clearAllMocks();
  np.hide = false;
});

describe('ConnectorTile — NETWORK_PROFILE gating (AT-558)', () => {
  it('standard profile: offers the authorization-code Connect button', () => {
    np.hide = false;
    const { onPrimary } = renderTile({});
    const btn = screen.getByRole('button', { name: /^connect$/i });
    expect(btn).toBeEnabled();
    fireEvent.click(btn);
    expect(onPrimary).toHaveBeenCalledTimes(1);
  });

  it('no_public_inbound: hides Connect, shows "Set up outbound access"', () => {
    np.hide = true;
    renderTile({});
    expect(screen.queryByRole('button', { name: /^connect$/i })).toBeNull();
    expect(
      screen.getByRole('button', { name: /set up outbound access/i }),
    ).toBeInTheDocument();
  });

  it('no_public_inbound: without a setup handler, the outbound button selects the tile (fallback)', () => {
    np.hide = true;
    const { onSelect, onPrimary } = renderTile({});
    fireEvent.click(
      screen.getByRole('button', { name: /set up outbound access/i }),
    );
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onPrimary).not.toHaveBeenCalled();
  });

  it('no_public_inbound: the outbound button pops the setup modal (onSetupOutbound), not select/OAuth', () => {
    np.hide = true;
    const onSetupOutbound = vi.fn();
    const { onSelect, onPrimary } = renderTile({ onSetupOutbound });
    fireEvent.click(
      screen.getByRole('button', { name: /set up outbound access/i }),
    );
    expect(onSetupOutbound).toHaveBeenCalledTimes(1);
    // When a setup handler is wired, the click must NOT fall back to select and
    // must never start the authorization-code flow.
    expect(onSelect).not.toHaveBeenCalled();
    expect(onPrimary).not.toHaveBeenCalled();
  });
});
