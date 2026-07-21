/**
 * TeamsChannelPicker — Teams channel selection tests (mirrors SlackChannelPicker).
 *
 *   - selectable channels load and render (name + team)
 *   - unconfigured workspace pre-selects all (read-all-granted default)
 *   - the selection is editable and the PATCH carries only the selected ids
 *   - the depth-phase consent copy is shown
 *   - the picker is shown in ConnectorDetailPanel only when Teams is connected
 *
 * Run:
 *   npx vitest run src/__tests__/TeamsChannelPicker.test.tsx
 */
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
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

import TeamsChannelPicker from '../components/integrations/TeamsChannelPicker';
import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';
import { Connector } from '../types/connector';

const AVAILABLE = [
  { id: 'T-eng/19:ops', name: 'ops-incidents', team: 'Engineering' },
  { id: 'T-eng/19:deploys', name: 'deploys', team: 'Engineering' },
];

function mockUnconfigured() {
  mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: [], configured: false });
  mockApiPatch.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['T-eng/19:ops'], configured: true });
}

describe('TeamsChannelPicker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUnconfigured();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('lists the selectable channels (name + team)', async () => {
    render(<TeamsChannelPicker />);
    expect(await screen.findByText('ops-incidents')).toBeInTheDocument();
    expect(screen.getByText('deploys')).toBeInTheDocument();
    expect(screen.getAllByText('Engineering').length).toBeGreaterThan(0);
  });

  it('pre-selects nothing when no selection has been saved yet', async () => {
    render(<TeamsChannelPicker />);
    await screen.findByText('ops-incidents');
    expect(screen.getByText('0 of 2 channels selected')).toBeInTheDocument();
  });

  it('sends only the selected channels in the PATCH (customer opts channels in)', async () => {
    render(<TeamsChannelPicker />);
    fireEvent.click(await screen.findByText('ops-incidents')); // select ops-incidents
    fireEvent.click(screen.getByText('Save channel selection'));

    await waitFor(() => expect(mockApiPatch).toHaveBeenCalledTimes(1));
    expect(mockApiPatch).toHaveBeenCalledWith('/api/connectors/teams/channels', {
      channels: ['T-eng/19:ops'],
    });
  });

  it('honours a previously-saved selection on load', async () => {
    mockApiGet.mockResolvedValue({
      ok: true, available: AVAILABLE, selected: ['T-eng/19:deploys'], configured: true,
    });
    render(<TeamsChannelPicker />);
    await screen.findByText('deploys');
    expect(screen.getByText('1 of 2 channels selected')).toBeInTheDocument();
  });

  it('shows the depth-phase message-content consent copy', async () => {
    render(<TeamsChannelPicker />);
    await screen.findByText('ops-incidents');
    expect(
      screen.getByText(/message content in selected channels is read and used as discovery evidence/i),
    ).toBeInTheDocument();
  });
});

// ── Placement inside ConnectorDetailPanel ─────────────────────────────────────

const teamsConnector: Connector = {
  id: 'teams',
  name: 'Microsoft Teams',
  category: 'Comms / docs',
  tier: 'standard',
  status: 'connected',
  configured: true,
  metrics: [],
  lastSynced: '1 hour ago',
  reads: ['Channels', 'Messages', 'Meetings'],
  signalStrength: 65,
};

describe('TeamsChannelPicker placement in ConnectorDetailPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUnconfigured();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders the channel picker when Teams is connected', async () => {
    render(<ConnectorDetailPanel connector={teamsConnector} onConfigure={vi.fn()} />);
    expect(await screen.findByText('Channels AgentIQ reads')).toBeInTheDocument();
  });

  it('does not render the channel picker when Teams is not connected', () => {
    render(
      <ConnectorDetailPanel
        connector={{ ...teamsConnector, status: 'not_connected', configured: false }}
        onConfigure={vi.fn()}
      />,
    );
    expect(screen.queryByText('Channels AgentIQ reads')).not.toBeInTheDocument();
  });
});
