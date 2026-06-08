/**
 * PostgreSQLScopePicker tests - T2-S12-A Task T5.
 *
 * Covers AC13 and AC14:
 * - connected PostgreSQL picker loads schema and saved scope
 * - scope save posts to the PostgreSQL scope endpoint
 * - viewers can see the picker, but Save is disabled with a tooltip
 * - ConnectorDetailPanel renders the picker only for connected PostgreSQL
 */

import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
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

import PostgreSQLScopePicker from '../components/integrations/PostgreSQLScopePicker';

const SCHEMA_DISCOVERY = {
  schemas: ['public', 'reporting'],
  tables: [
    { schema: 'public', table: 'service_tickets' },
    { schema: 'public', table: 'incidents' },
    { schema: 'reporting', table: 'ticket_rollups' },
  ],
};

const SAVED_SCOPE = {
  schemas: ['public'],
  tables: ['public.service_tickets'],
};

function setupDefaultMocks() {
  mocks.apiGet.mockImplementation((url: string) => {
    if (url.includes('/schema')) return Promise.resolve(SCHEMA_DISCOVERY);
    if (url.includes('/scope')) return Promise.resolve(null);
    return Promise.resolve(null);
  });
  mocks.apiPost.mockResolvedValue({ schemas: [], tables: [] });
}

const postgresqlConnector = {
  id: 'postgresql',
  name: 'PostgreSQL',
  status: 'connected' as const,
  configured: true,
  category: 'Database',
  tier: 'recommended' as const,
  reads: ['service_tickets'],
  lastSynced: '1 hour ago',
  metrics: [],
  signalStrength: 80,
};

describe('PostgreSQLScopePicker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('AC13: renders schemas after loading discovery data', async () => {
    render(<PostgreSQLScopePicker />);

    await waitFor(() => {
      expect(screen.getByText('public')).toBeInTheDocument();
      expect(screen.getByText('reporting')).toBeInTheDocument();
    });
  });

  it('AC13: loads schema and saved scope endpoints on mount', async () => {
    render(<PostgreSQLScopePicker />);

    await waitFor(() => {
      expect(mocks.apiGet).toHaveBeenCalledWith('/api/db-connectors/postgresql/schema');
      expect(mocks.apiGet).toHaveBeenCalledWith('/api/db-connectors/postgresql/scope');
    });
  });

  it('AC13: pre-populates a previously saved PostgreSQL scope', async () => {
    mocks.apiGet.mockImplementation((url: string) => {
      if (url.includes('/schema')) return Promise.resolve(SCHEMA_DISCOVERY);
      if (url.includes('/scope')) return Promise.resolve(SAVED_SCOPE);
      return Promise.resolve(null);
    });

    render(<PostgreSQLScopePicker />);

    await waitFor(() => {
      expect(screen.getByText('1 schema, 1 table declared')).toBeInTheDocument();
    });
  });

  it('AC13: saves selected schemas and tables to the PostgreSQL scope endpoint', async () => {
    render(<PostgreSQLScopePicker />);
    await waitFor(() => screen.getByText('public'));

    await act(async () => {
      fireEvent.click(screen.getAllByRole('checkbox')[0]);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save scope declaration/i }));
    });

    await waitFor(() => {
      expect(mocks.apiPost).toHaveBeenCalledWith(
        '/api/db-connectors/postgresql/scope',
        {
          schemas: ['public'],
          tables: ['public.service_tickets', 'public.incidents'],
        },
      );
    });
  });

  it('AC14: viewer users see the picker but Save is disabled with tooltip', async () => {
    render(<PostgreSQLScopePicker viewerOnly />);
    await waitFor(() => screen.getByText('public'));

    const saveBtn = screen.getByRole('button', { name: /save scope declaration/i });
    expect(saveBtn).toBeDisabled();
    expect(saveBtn).toHaveAttribute('title', 'Analyst role required to save scope');
  });

  it('AC14: viewer users cannot post scope changes', async () => {
    render(<PostgreSQLScopePicker viewerOnly />);
    await waitFor(() => screen.getByText('public'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save scope declaration/i }));
    });

    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('AC13: ConnectorDetailPanel renders PostgreSQLScopePicker for connected PostgreSQL', async () => {
    const { default: ConnectorDetailPanel } = await import(
      '../components/integrations/ConnectorDetailPanel'
    );

    render(
      <ConnectorDetailPanel connector={postgresqlConnector} onConfigure={vi.fn()} />,
    );

    await waitFor(() => {
      expect(screen.getByText('PostgreSQL scope')).toBeInTheDocument();
    });
  });

  it('AC14: ConnectorDetailPanel passes viewerOnly to PostgreSQLScopePicker for viewer users', async () => {
    vi.stubEnv('VITE_DEV_JWT_ROLE', 'viewer');
    const { default: ConnectorDetailPanel } = await import(
      '../components/integrations/ConnectorDetailPanel'
    );

    render(
      <ConnectorDetailPanel connector={postgresqlConnector} onConfigure={vi.fn()} />,
    );

    await waitFor(() => {
      expect(screen.getByText('PostgreSQL scope')).toBeInTheDocument();
    });

    const saveBtn = screen.getByRole('button', { name: /save scope declaration/i });
    expect(saveBtn).toBeDisabled();
    expect(saveBtn).toHaveAttribute('title', 'Analyst role required to save scope');
  });

  it('AC13: ConnectorDetailPanel does not render picker when PostgreSQL is disconnected', async () => {
    const { default: ConnectorDetailPanel } = await import(
      '../components/integrations/ConnectorDetailPanel'
    );

    render(
      <ConnectorDetailPanel
        connector={{ ...postgresqlConnector, status: 'disconnected', configured: false }}
        onConfigure={vi.fn()}
      />,
    );

    expect(screen.queryByText('PostgreSQL scope')).not.toBeInTheDocument();
  });
});
