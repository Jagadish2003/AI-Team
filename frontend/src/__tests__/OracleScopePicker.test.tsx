/**
 * OracleScopePicker — T2-S12-A Task T4
 * Vitest / Testing Library tests
 *
 * Acceptance criteria verified:
 *   AC12 — Renders when connector.id === 'oracle_db' && connected.
 *           Loads schema, allows table selection, saves scope,
 *           pre-populates saved selection.
 *   AC14 — Viewer sees picker but Save is disabled with tooltip.
 *   AC18 — Case-sensitivity tooltip text is present verbatim.
 *
 * Run:
 *   npx vitest run src/__tests__/OracleScopePicker.test.tsx
 */

import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Mock API client before importing the component ────────────────────────────

const mockApiGet = vi.fn();
const mockApiPost = vi.fn();

vi.mock('../lib/apiClient', () => ({
  ApiError: class ApiError extends Error {
    body: unknown;
    constructor(message: string, body: unknown) {
      super(message);
      this.body = body;
    }
  },
  apiGet: (...args: unknown[]) => mockApiGet(...args),
  apiPost: (...args: unknown[]) => mockApiPost(...args),
}));

vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: mockPush }),
}));

const mockPush = vi.fn();

import OracleScopePicker from '../components/integrations/OracleScopePicker';

// ── Fixtures ──────────────────────────────────────────────────────────────────

const SCHEMA_DISCOVERY = {
  schemas: ['HR', 'OPERATIONS'],
  tables: [
    { schema: 'HR', table: 'SERVICE_TICKETS' },
    { schema: 'HR', table: 'EMPLOYEES' },
    { schema: 'OPERATIONS', table: 'INCIDENTS' },
  ],
};

const SAVED_SCOPE = {
  schemas: ['HR'],
  tables: ['HR.SERVICE_TICKETS'],
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function setupDefaultMocks() {
  mockApiGet.mockImplementation((url: string) => {
    if (url.includes('/schema')) return Promise.resolve(SCHEMA_DISCOVERY);
    if (url.includes('/scope')) return Promise.resolve(null);
    return Promise.resolve(null);
  });
  mockApiPost.mockResolvedValue({ schemas: [], tables: [] });
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('OracleScopePicker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  // ── AC12: renders and loads schema ──────────────────────────────────────────

  it('AC12: renders schema names after loading', async () => {
    render(<OracleScopePicker />);

    await waitFor(() => {
      expect(screen.getByText('HR')).toBeInTheDocument();
      expect(screen.getByText('OPERATIONS')).toBeInTheDocument();
    });
  });

  it('AC12: calls /api/db-connectors/oracle_db/schema on mount', async () => {
    render(<OracleScopePicker />);

    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledWith('/api/db-connectors/oracle_db/schema');
    });
  });

  it('AC12: calls /api/db-connectors/oracle_db/scope on mount', async () => {
    render(<OracleScopePicker />);

    await waitFor(() => {
      expect(mockApiGet).toHaveBeenCalledWith('/api/db-connectors/oracle_db/scope');
    });
  });

  it('AC12: shows loading state initially', () => {
    // Make schema request hang
    mockApiGet.mockImplementation(() => new Promise(() => {}));
    render(<OracleScopePicker />);
    expect(screen.getByText(/loading schema discovery/i)).toBeInTheDocument();
  });

  it('AC12: shows error state when schema request fails', async () => {
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/schema')) return Promise.resolve(null);
      return Promise.resolve(null);
    });

    render(<OracleScopePicker />);

    await waitFor(() => {
      expect(screen.getByText(/no schemas discovered/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    });
  });

  it('AC12: retry button reloads schema', async () => {
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/schema')) return Promise.resolve(null);
      return Promise.resolve(null);
    });

    render(<OracleScopePicker />);

    await waitFor(() => screen.getByRole('button', { name: /retry/i }));

    // Fix the mock so the retry succeeds
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/schema')) return Promise.resolve(SCHEMA_DISCOVERY);
      return Promise.resolve(null);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    });

    await waitFor(() => {
      expect(screen.getByText('HR')).toBeInTheDocument();
    });
  });

  // ── AC12: pre-populate saved scope ──────────────────────────────────────────

  it('AC12: pre-populates previously saved scope on load', async () => {
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/schema')) return Promise.resolve(SCHEMA_DISCOVERY);
      if (url.includes('/scope')) return Promise.resolve(SAVED_SCOPE);
      return Promise.resolve(null);
    });

    render(<OracleScopePicker />);

    await waitFor(() => {
      // HR schema should be checked (aria-checked=true)
      const schemaCheckbox = screen.getByRole('checkbox', { name: /select schema hr/i });
      expect(schemaCheckbox).toHaveAttribute('aria-checked', 'true');
    });
  });

  it('AC12: shows declared count when selection is active', async () => {
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/schema')) return Promise.resolve(SCHEMA_DISCOVERY);
      if (url.includes('/scope')) return Promise.resolve(SAVED_SCOPE);
      return Promise.resolve(null);
    });

    render(<OracleScopePicker />);

    await waitFor(() => {
      // The declared count paragraph includes both schema and table counts
      expect(screen.getByText(/1 schema.*1 table/i)).toBeInTheDocument();
    });
  });

  // ── AC12: schema/table selection ────────────────────────────────────────────

  it('AC12: clicking a schema checkbox selects it', async () => {
    render(<OracleScopePicker />);
    await waitFor(() => screen.getByText('HR'));

    const schemaCheckbox = screen.getByRole('checkbox', { name: /select schema hr/i });
    await act(async () => { fireEvent.click(schemaCheckbox); });

    expect(schemaCheckbox).toHaveAttribute('aria-checked', 'true');
  });

  it('AC12: clicking a selected schema deselects it', async () => {
    render(<OracleScopePicker />);
    await waitFor(() => screen.getByText('HR'));

    const schemaCheckbox = screen.getByRole('checkbox', { name: /select schema hr/i });
    // Select then deselect
    await act(async () => { fireEvent.click(schemaCheckbox); });
    await act(async () => { fireEvent.click(schemaCheckbox); });

    expect(schemaCheckbox).toHaveAttribute('aria-checked', 'false');
  });

  it('AC12: expand button shows tables for a schema', async () => {
    render(<OracleScopePicker />);
    await waitFor(() => screen.getByText('HR'));

    const expandBtn = screen.getByRole('button', { name: /expand hr/i });
    await act(async () => { fireEvent.click(expandBtn); });

    expect(screen.getByRole('checkbox', { name: /select table service_tickets/i })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /select table employees/i })).toBeInTheDocument();
  });

  // ── AC12: save scope ────────────────────────────────────────────────────────

  it('AC12: Save button calls POST /api/db-connectors/oracle_db/scope', async () => {
    render(<OracleScopePicker />);
    await waitFor(() => screen.getByText('HR'));

    // Select HR schema
    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: /select schema hr/i }));
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save scope declaration/i }));
    });

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalledWith(
        '/api/db-connectors/oracle_db/scope',
        expect.objectContaining({ schemas: expect.arrayContaining(['HR']) }),
      );
    });
  });

  it('AC12: successful save shows toast notification', async () => {
    render(<OracleScopePicker />);
    await waitFor(() => screen.getByText('HR'));

    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: /select schema hr/i }));
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save scope declaration/i }));
    });

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith(expect.stringContaining('Scope saved'));
    });
  });

  it('AC12: onSaved callback is called after successful save', async () => {
    const onSaved = vi.fn();
    render(<OracleScopePicker onSaved={onSaved} />);
    await waitFor(() => screen.getByText('HR'));

    await act(async () => {
      fireEvent.click(screen.getByRole('checkbox', { name: /select schema hr/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save scope declaration/i }));
    });

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  });

  // ── AC14: viewerOnly disables save ──────────────────────────────────────────

  it('AC14: Save button is disabled when viewerOnly=true', async () => {
    render(<OracleScopePicker viewerOnly />);
    await waitFor(() => screen.getByText('HR'));

    const saveBtn = screen.getByRole('button', { name: /save scope declaration/i });
    expect(saveBtn).toBeDisabled();
  });

  it('AC14: Save button shows Analyst role required tooltip when viewerOnly=true', async () => {
    render(<OracleScopePicker viewerOnly />);
    await waitFor(() => screen.getByText('HR'));

    const saveBtn = screen.getByRole('button', { name: /save scope declaration/i });
    expect(saveBtn).toHaveAttribute('title', 'Analyst role required');
  });

  it('AC14: clicking save when viewerOnly=true does not POST', async () => {
    render(<OracleScopePicker viewerOnly />);
    await waitFor(() => screen.getByText('HR'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save scope declaration/i }));
    });

    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it('AC14: Viewer can still see the picker and schema list', async () => {
    render(<OracleScopePicker viewerOnly />);

    await waitFor(() => {
      expect(screen.getByText('HR')).toBeInTheDocument();
      expect(screen.getByText('OPERATIONS')).toBeInTheDocument();
    });
  });

  // ── AC18: case-sensitivity tooltip ──────────────────────────────────────────

  it('AC18: case-sensitivity text is present in the rendered output', async () => {
    render(<OracleScopePicker />);

    await waitFor(() => {
      // The exact required text per AC18
      expect(
        screen.getByText(/Oracle schema names are case-sensitive/i),
      ).toBeInTheDocument();
    });
  });

  it('AC18: case-sensitivity message mentions stored in database', async () => {
    render(<OracleScopePicker />);

    await waitFor(() => {
      expect(
        screen.getByText(/shown exactly as stored in the database/i),
      ).toBeInTheDocument();
    });
  });

  it('AC18: schema names are displayed verbatim without normalisation', async () => {
    // Oracle typically uses UPPERCASE — verify it is NOT lowercased
    const mixedCaseDiscovery = {
      schemas: ['HR', 'MySchema'],
      tables: [
        { schema: 'HR', table: 'SERVICE_TICKETS' },
        { schema: 'MySchema', table: 'Incidents' },
      ],
    };
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes('/schema')) return Promise.resolve(mixedCaseDiscovery);
      return Promise.resolve(null);
    });

    render(<OracleScopePicker />);

    await waitFor(() => {
      // Names must appear exactly as returned — no lowercasing
      expect(screen.getByText('HR')).toBeInTheDocument();
      expect(screen.getByText('MySchema')).toBeInTheDocument();
    });
  });

  // ── ConnectorDetailPanel wiring (AC12) ─────────────────────────────────────

  it('AC12: ConnectorDetailPanel renders OracleScopePicker for oracle_db connected connector', async () => {
    // Import ConnectorDetailPanel and verify it renders OracleScopePicker
    const { default: ConnectorDetailPanel } = await import(
      '../components/integrations/ConnectorDetailPanel'
    );

    const oracleConnector = {
      id: 'oracle_db',
      name: 'Oracle DB',
      status: 'connected' as const,
      configured: true,
      category: 'Database',
      tier: 'recommended' as const,
      reads: ['ServiceTickets'],
      lastSynced: '1 hour ago',
      metrics: [],
      signalStrength: 80,
    };

    render(
      <ConnectorDetailPanel connector={oracleConnector} onConfigure={vi.fn()} />,
    );

    // OracleScopePicker section header should appear
    await waitFor(() => {
      expect(screen.getByText('Oracle DB scope')).toBeInTheDocument();
    });
  });

  it('AC12: ConnectorDetailPanel does NOT render OracleScopePicker when connector is disconnected', async () => {
    const { default: ConnectorDetailPanel } = await import(
      '../components/integrations/ConnectorDetailPanel'
    );

    const disconnectedOracle = {
      id: 'oracle_db',
      name: 'Oracle DB',
      status: 'disconnected' as const,
      configured: false,
      category: 'Database',
      tier: 'recommended' as const,
      reads: ['ServiceTickets'],
      lastSynced: '-',
      metrics: [],
      signalStrength: 0,
    };

    render(
      <ConnectorDetailPanel connector={disconnectedOracle} onConfigure={vi.fn()} />,
    );

    // Should not render the picker at all
    expect(screen.queryByText('Oracle DB scope')).not.toBeInTheDocument();
  });
});
