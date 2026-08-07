/**
 * JiraProjectPicker — Jira multi-project selection tests.
 *
 * Verifies the customer can choose which Jira projects AgentIQ scopes discovery to:
 *   - selectable projects load and render (name + key)
 *   - multi-select: choosing several and saving PATCHes { projects: [...] }
 *   - a previously-saved selection pre-selects on load
 *   - the picker is shown in ConnectorDetailPanel only when Jira is connected
 *
 * Run:
 *   npx vitest run src/__tests__/JiraProjectPicker.test.tsx
 */
import '@testing-library/jest-dom/vitest';
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
// The picker reads its projects through the shared data cache (see
// usePickerResource), which the app provides at its root — so these tests mount a
// provider too. A fresh one per render keeps each test's cache isolated.
import { renderWithCache as render } from '../test-utils/renderWithCache';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

const mockApiGet = vi.fn();
const mockApiPatch = vi.fn();
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
  apiPatch: (...args: unknown[]) => mockApiPatch(...args),
  apiPost: (...args: unknown[]) => mockApiPost(...args),
}));

const mockPush = vi.fn();
vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: mockPush }),
}));

import JiraProjectPicker from '../components/integrations/JiraProjectPicker';
import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';
import { Connector } from '../types/connector';

const AVAILABLE = [
  { key: 'CRM', name: 'Customer Platform' },
  { key: 'OPS', name: 'Operations' },
];

function mockUnconfigured() {
  mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: [], configured: false });
  mockApiPatch.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['CRM'], configured: true });
}

describe('JiraProjectPicker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUnconfigured();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('lists the selectable projects (name + key)', async () => {
    render(<JiraProjectPicker />);
    expect(await screen.findByText('Customer Platform')).toBeInTheDocument();
    expect(screen.getByText('Operations')).toBeInTheDocument();
    expect(screen.getByText('CRM')).toBeInTheDocument();
  });

  it('pre-selects nothing when no selection has been saved yet', async () => {
    render(<JiraProjectPicker />);
    await screen.findByText('Customer Platform');
    expect(screen.getByText('0 of 2 projects selected')).toBeInTheDocument();
  });

  it('multi-select: saving PATCHes all chosen project keys', async () => {
    render(<JiraProjectPicker />);
    fireEvent.click(await screen.findByText('Customer Platform'));
    fireEvent.click(screen.getByText('Operations'));
    fireEvent.click(screen.getByText('Save project selection'));

    await waitFor(() => expect(mockApiPatch).toHaveBeenCalledTimes(1));
    expect(mockApiPatch).toHaveBeenCalledWith('/api/connectors/jira/projects', {
      projects: ['CRM', 'OPS'],
    });
  });

  it('toggling a project on and off keeps the others (multi-select)', async () => {
    render(<JiraProjectPicker />);
    const crm = await screen.findByRole('checkbox', { name: /Customer Platform/ });
    const ops = screen.getByRole('checkbox', { name: /Operations/ });
    fireEvent.click(crm);
    fireEvent.click(ops);
    expect(crm).toHaveAttribute('aria-checked', 'true');
    expect(ops).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(crm); // toggle CRM back off
    expect(crm).toHaveAttribute('aria-checked', 'false');
    expect(ops).toHaveAttribute('aria-checked', 'true');
  });

  it('honours a previously-saved selection on load', async () => {
    mockApiGet.mockResolvedValue({
      ok: true, available: AVAILABLE, selected: ['OPS'], configured: true,
    });
    render(<JiraProjectPicker />);
    const ops = await screen.findByRole('checkbox', { name: /Operations/ });
    expect(ops).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('1 of 2 projects selected')).toBeInTheDocument();
  });
});

// ── Placement inside ConnectorDetailPanel ─────────────────────────────────────

const jiraConnector: Connector = {
  id: 'jira',
  name: 'Jira',
  category: 'Issues / backlog',
  tier: 'standard',
  status: 'connected',
  configured: true,
  metrics: [],
  lastSynced: '1 hour ago',
  reads: ['Issues', 'Sprints', 'Epics'],
  signalStrength: 78,
};

describe('JiraProjectPicker placement in ConnectorDetailPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUnconfigured();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders the project picker when Jira is connected', async () => {
    render(<ConnectorDetailPanel connector={jiraConnector} onConfigure={vi.fn()} />);
    expect(await screen.findByText('Projects AgentIQ reads')).toBeInTheDocument();
  });

  it('does not render the project picker when Jira is not connected', () => {
    render(
      <ConnectorDetailPanel
        connector={{ ...jiraConnector, status: 'not_connected', configured: false }}
        onConfigure={vi.fn()}
      />,
    );
    expect(screen.queryByText('Projects AgentIQ reads')).not.toBeInTheDocument();
  });
});
