import React from 'react';
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConnectorProvider, useConnectorContext } from '../context/ConnectorContext';
import { DataCacheProvider } from '../lib/dataCache';
import type { Connector } from '../types/connector';

function ConnectorProbe() {
  const { all, error, loading, selectedConnectorId } = useConnectorContext();

  if (loading) return <div>Loading connectors</div>;
  if (error) return <div>{error}</div>;

  return (
    <output data-testid="connectors">
      {JSON.stringify({ all, selectedConnectorId })}
    </output>
  );
}

// Probe that surfaces the loading flag + a refetch trigger, to assert that a
// background refetch does NOT flip `loading` (which would unmount the hub — the
// "page refresh" that remounted open modals; see IntegrationHubPage's
// {loading && <LoadingPanel/>} gate).
function LoadingProbe() {
  const { loading, refetch } = useConnectorContext();
  return (
    <div>
      <span data-testid="loading">{loading ? 'loading' : 'idle'}</span>
      <button onClick={() => refetch()}>refetch</button>
    </div>
  );
}

describe('ConnectorProvider', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => [
          {
            id: 'salesforce',
            name: 'Salesforce',
            status: 'connected',
            metrics: [{ label: 'Loans', value: '-' }],
            lastSynced: 'Just now',
            signalStrength: 100,
            org_id: 'default',
          },
          {
            id: 'jira',
            name: 'Jira',
            status: 'disconnected',
          },
        ],
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('normalizes partial connector payloads before exposing them to Integration Hub', async () => {
    render(
      <DataCacheProvider>
        <ConnectorProvider>
          <ConnectorProbe />
        </ConnectorProvider>
      </DataCacheProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('connectors')).toBeInTheDocument();
    });

    const payload = JSON.parse(screen.getByTestId('connectors').textContent ?? '{}') as {
      all: Connector[];
      selectedConnectorId: string | null;
    };

    const salesforce = payload.all.find((connector) => connector.id === 'salesforce');
    const jira = payload.all.find((connector) => connector.id === 'jira');

    expect(salesforce?.reads).toEqual([]);
    expect(salesforce?.metrics).toEqual([{ label: 'Loans', value: '-' }]);
    expect(salesforce?.configured).toBe(false);

    expect(jira?.reads).toEqual([]);
    expect(jira?.metrics).toEqual([]);
    expect(jira?.category).toBe('General');
    expect(jira?.tier).toBe('standard');
  });

  it('does not flip loading on a background refetch once data is present', async () => {
    render(
      <DataCacheProvider>
        <ConnectorProvider>
          <LoadingProbe />
        </ConnectorProvider>
      </DataCacheProvider>,
    );

    // First load resolves → idle.
    await waitFor(() => expect(screen.getByTestId('loading').textContent).toBe('idle'));

    // Trigger a refetch (what a mutation's invalidate does). Loading must STAY
    // idle — a flip to 'loading' would unmount the hub (page-refresh + modal
    // reopen regression).
    fireEvent.click(screen.getByRole('button', { name: 'refetch' }));
    // Give the microtask-coalesced invalidate + refetch time to run.
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.getByTestId('loading').textContent).toBe('idle');
  });
});
