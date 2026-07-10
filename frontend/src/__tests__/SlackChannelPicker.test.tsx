/**
 * SlackChannelPicker — R18-C0 P5 tests
 *
 * Verifies the customer can choose which Slack channels AgentIQ reads:
 *   - selectable channels load and render
 *   - unconfigured workspace pre-selects all (reflects current read-all default)
 *   - the selection is editable and the PATCH carries only the selected ids
 *   - the picker is shown in ConnectorDetailPanel only when Slack is connected
 *
 * Run:
 *   npx vitest run src/__tests__/SlackChannelPicker.test.tsx
 */
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Mock API client before importing the component ────────────────────────────

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

import SlackChannelPicker from '../components/integrations/SlackChannelPicker';
import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';
import { Connector } from '../types/connector';

const AVAILABLE = [
  { id: 'C001', name: 'ops-incidents' },
  { id: 'C002', name: 'deploys' },
];

function mockUnconfigured() {
  mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: [], configured: false });
  mockApiPatch.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['C001'], configured: true });
}

describe('SlackChannelPicker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUnconfigured();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('lists the selectable channels', async () => {
    render(<SlackChannelPicker />);
    expect(await screen.findByText('#ops-incidents')).toBeInTheDocument();
    expect(screen.getByText('#deploys')).toBeInTheDocument();
  });

  it('pre-selects all channels when no selection has been saved yet', async () => {
    render(<SlackChannelPicker />);
    await screen.findByText('#ops-incidents');
    // Both start checked (reflects the read-all default the customer can narrow).
    expect(screen.getByText('2 of 2 channels selected')).toBeInTheDocument();
  });

  it('sends only the selected channels in the PATCH (customer narrows the set)', async () => {
    render(<SlackChannelPicker />);
    // De-select 'deploys' so only 'ops-incidents' remains selected.
    const deploys = await screen.findByText('#deploys');
    fireEvent.click(deploys);
    fireEvent.click(screen.getByText('Save channel selection'));

    await waitFor(() => expect(mockApiPatch).toHaveBeenCalledTimes(1));
    expect(mockApiPatch).toHaveBeenCalledWith(
      '/api/connectors/slack/channels',
      { channels: ['C001'] },
    );
  });

  it('honours a previously-saved selection on load', async () => {
    mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['C002'], configured: true });
    render(<SlackChannelPicker />);
    await screen.findByText('#deploys');
    expect(screen.getByText('1 of 2 channels selected')).toBeInTheDocument();
  });
});

// ── Placement inside ConnectorDetailPanel ─────────────────────────────────────

const slackConnector: Connector = {
  id: 'slack',
  name: 'Slack',
  category: 'Comms · Ops',
  tier: 'standard',
  status: 'connected',
  configured: true,
  metrics: [],
  lastSynced: '1 hour ago',
  reads: ['Channels', 'Threads', 'Mentions'],
  signalStrength: 70,
};

describe('SlackChannelPicker placement in ConnectorDetailPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUnconfigured();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders the channel picker when Slack is connected', async () => {
    render(<ConnectorDetailPanel connector={slackConnector} onConfigure={vi.fn()} />);
    expect(await screen.findByText('Channels AgentIQ reads')).toBeInTheDocument();
  });

  it('does not render the channel picker when Slack is not connected', () => {
    render(
      <ConnectorDetailPanel
        connector={{ ...slackConnector, status: 'not_connected', configured: false }}
        onConfigure={vi.fn()}
      />,
    );
    expect(screen.queryByText('Channels AgentIQ reads')).not.toBeInTheDocument();
  });
});
