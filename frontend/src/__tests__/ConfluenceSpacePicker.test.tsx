/**
 * ConfluenceSpacePicker — multi-space selection tests.
 *
 * Run: npx vitest run src/__tests__/ConfluenceSpacePicker.test.tsx
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
    constructor(message: string, body: unknown) { super(message); this.body = body; }
  },
  apiGet: (...a: unknown[]) => mockApiGet(...a),
  apiPatch: (...a: unknown[]) => mockApiPatch(...a),
  apiPost: (...a: unknown[]) => mockApiPost(...a),
}));

const mockPush = vi.fn();
vi.mock('../components/common/Toast', () => ({ useToast: () => ({ push: mockPush }) }));

import ConfluenceSpacePicker from '../components/integrations/ConfluenceSpacePicker';
import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';
import { Connector } from '../types/connector';

const AVAILABLE = [
  { key: 'ENG', name: 'Engineering' },
  { key: 'OPS', name: 'Operations' },
];

function mockUnconfigured() {
  mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: [], configured: false });
  mockApiPatch.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['ENG'], configured: true });
}

describe('ConfluenceSpacePicker', () => {
  beforeEach(() => { vi.clearAllMocks(); mockUnconfigured(); });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('lists the selectable spaces (name + key)', async () => {
    render(<ConfluenceSpacePicker />);
    expect(await screen.findByText('Engineering')).toBeInTheDocument();
    expect(screen.getByText('Operations')).toBeInTheDocument();
    expect(screen.getByText('ENG')).toBeInTheDocument();
  });

  it('multi-select: saving PATCHes all chosen space keys', async () => {
    render(<ConfluenceSpacePicker />);
    fireEvent.click(await screen.findByText('Engineering'));
    fireEvent.click(screen.getByText('Operations'));
    fireEvent.click(screen.getByText('Save space selection'));
    await waitFor(() => expect(mockApiPatch).toHaveBeenCalledTimes(1));
    expect(mockApiPatch).toHaveBeenCalledWith('/api/connectors/confluence/spaces', {
      spaces: ['ENG', 'OPS'],
    });
  });

  it('honours a previously-saved selection on load', async () => {
    mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['OPS'], configured: true });
    render(<ConfluenceSpacePicker />);
    const ops = await screen.findByRole('checkbox', { name: /Operations/ });
    expect(ops).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('1 of 2 spaces selected')).toBeInTheDocument();
  });
});

const confluenceConnector: Connector = {
  id: 'confluence', name: 'Confluence', category: 'Docs', tier: 'standard',
  status: 'connected', configured: true, metrics: [], lastSynced: '1 hour ago',
  reads: ['Spaces', 'Pages'], signalStrength: 60,
};

describe('ConfluenceSpacePicker placement in ConnectorDetailPanel', () => {
  beforeEach(() => { vi.clearAllMocks(); mockUnconfigured(); });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('renders the space picker when Confluence is connected', async () => {
    render(<ConnectorDetailPanel connector={confluenceConnector} onConfigure={vi.fn()} />);
    expect(await screen.findByText('Spaces AgentIQ reads')).toBeInTheDocument();
  });

  it('does not render the space picker when Confluence is not connected', () => {
    render(
      <ConnectorDetailPanel
        connector={{ ...confluenceConnector, status: 'not_connected', configured: false }}
        onConfigure={vi.fn()}
      />,
    );
    expect(screen.queryByText('Spaces AgentIQ reads')).not.toBeInTheDocument();
  });
});
