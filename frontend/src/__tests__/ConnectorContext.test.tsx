import React from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConnectorProvider, useConnectorContext } from '../context/ConnectorContext';
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
      <ConnectorProvider>
        <ConnectorProbe />
      </ConnectorProvider>,
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
});
