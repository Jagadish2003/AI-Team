/**
 * ConnectorDetailPanel wiring tests - T2-S12-A Task T6.
 *
 * The branch responsibility is to place OracleScopePicker and
 * PostgreSQLScopePicker at the same insertion point as the existing database
 * scope picker, and to preserve AC14 viewer-only save behavior.
 */

import '@testing-library/jest-dom/vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  push: vi.fn(),
}));

vi.mock('../lib/apiClient', () => ({
  ApiError: class ApiError extends Error {
    body: unknown;
    constructor(message: string, status: number, body: unknown) {
      super(message);
      this.body = body;
    }
  },
  apiGet: (...args: unknown[]) => mocks.apiGet(...args),
  apiPost: (...args: unknown[]) => mocks.apiPost(...args),
}));

vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: mocks.push }),
}));

import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';

function connector(id: 'oracle_db' | 'postgresql', status: 'connected' | 'disconnected' = 'connected') {
  return {
    id,
    name: id === 'oracle_db' ? 'Oracle DB' : 'PostgreSQL',
    status,
    configured: status === 'connected',
    category: 'Database',
    tier: 'recommended' as const,
    reads: ['service_tickets'],
    lastSynced: status === 'connected' ? '1 hour ago' : '-',
    metrics: [],
    signalStrength: status === 'connected' ? 80 : 0,
  };
}

function setupApiMocks() {
  mocks.apiGet.mockImplementation((url: string) => {
    if (url.includes('/oracle_db/schema')) {
      return Promise.resolve({
        schemas: ['HR'],
        tables: [{ schema: 'HR', table: 'SERVICE_TICKETS' }],
      });
    }
    if (url.includes('/postgresql/schema')) {
      return Promise.resolve({
        schemas: ['public'],
        tables: [{ schema: 'public', table: 'service_tickets' }],
      });
    }
    if (url.includes('/scope')) return Promise.resolve(null);
    return Promise.resolve(null);
  });
  mocks.apiPost.mockResolvedValue({ schemas: [], tables: [] });
}

describe('T2-S12-A T6 Oracle/PostgreSQL scope picker wiring', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupApiMocks();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('AC12: renders OracleScopePicker for connected oracle_db connector', async () => {
    render(<ConnectorDetailPanel connector={connector('oracle_db')} onConfigure={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Oracle DB scope')).toBeInTheDocument();
      expect(screen.getByText('HR')).toBeInTheDocument();
    });
  });

  it('AC13: renders PostgreSQLScopePicker for connected postgresql connector', async () => {
    render(<ConnectorDetailPanel connector={connector('postgresql')} onConfigure={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('PostgreSQL scope')).toBeInTheDocument();
      expect(screen.getByText('public')).toBeInTheDocument();
    });
  });

  it('AC12/AC13: does not render either picker when connector is disconnected', () => {
    const { rerender } = render(
      <ConnectorDetailPanel connector={connector('oracle_db', 'disconnected')} onConfigure={vi.fn()} />,
    );
    expect(screen.queryByText('Oracle DB scope')).not.toBeInTheDocument();

    rerender(
      <ConnectorDetailPanel connector={connector('postgresql', 'disconnected')} onConfigure={vi.fn()} />,
    );
    expect(screen.queryByText('PostgreSQL scope')).not.toBeInTheDocument();
  });

  it('AC14: viewer users see Oracle picker but cannot save', async () => {
    vi.stubEnv('VITE_DEV_JWT_ROLE', 'viewer');
    render(<ConnectorDetailPanel connector={connector('oracle_db')} onConfigure={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Oracle DB scope')).toBeInTheDocument();
    });

    const saveBtn = screen.getByRole('button', { name: /save scope declaration/i });
    expect(saveBtn).toBeDisabled();
    expect(saveBtn).toHaveAttribute('title', 'Analyst role required');
  });

  it('AC14: viewer users see PostgreSQL picker but cannot save', async () => {
    vi.stubEnv('VITE_DEV_JWT_ROLE', 'viewer');
    render(<ConnectorDetailPanel connector={connector('postgresql')} onConfigure={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('PostgreSQL scope')).toBeInTheDocument();
    });

    const saveBtn = screen.getByRole('button', { name: /save scope declaration/i });
    expect(saveBtn).toBeDisabled();
    expect(saveBtn).toHaveAttribute('title', 'Analyst role required');
  });
});
