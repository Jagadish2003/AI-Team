/**
 * SharePointSitePicker — multi-site selection tests.
 *
 * Run: npx vitest run src/__tests__/SharePointSitePicker.test.tsx
 */
import '@testing-library/jest-dom/vitest';
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
// The picker reads its sites through the shared data cache (see
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
    constructor(message: string, body: unknown) { super(message); this.body = body; }
  },
  apiGet: (...a: unknown[]) => mockApiGet(...a),
  apiPatch: (...a: unknown[]) => mockApiPatch(...a),
  apiPost: (...a: unknown[]) => mockApiPost(...a),
}));

const mockPush = vi.fn();
vi.mock('../components/common/Toast', () => ({ useToast: () => ({ push: mockPush }) }));

import SharePointSitePicker from '../components/integrations/SharePointSitePicker';
import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';
import { Connector } from '../types/connector';

const AVAILABLE = [
  { id: 'S-eng', name: 'Engineering' },
  { id: 'S-ops', name: 'Operations' },
];

function mockUnconfigured() {
  mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: [], configured: false });
  mockApiPatch.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['S-eng'], configured: true });
}

describe('SharePointSitePicker', () => {
  beforeEach(() => { vi.clearAllMocks(); mockUnconfigured(); });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('lists the selectable sites', async () => {
    render(<SharePointSitePicker />);
    expect(await screen.findByText('Engineering')).toBeInTheDocument();
    expect(screen.getByText('Operations')).toBeInTheDocument();
  });

  it('multi-select: saving PATCHes all chosen site ids', async () => {
    render(<SharePointSitePicker />);
    fireEvent.click(await screen.findByText('Engineering'));
    fireEvent.click(screen.getByText('Operations'));
    fireEvent.click(screen.getByText('Save site selection'));
    await waitFor(() => expect(mockApiPatch).toHaveBeenCalledTimes(1));
    expect(mockApiPatch).toHaveBeenCalledWith('/api/connectors/sharepoint/sites', {
      sites: ['S-eng', 'S-ops'],
    });
  });

  it('honours a previously-saved selection on load', async () => {
    mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['S-ops'], configured: true });
    render(<SharePointSitePicker />);
    const ops = await screen.findByRole('checkbox', { name: /Operations/ });
    expect(ops).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('1 of 2 sites selected')).toBeInTheDocument();
  });
});

const sharepointConnector: Connector = {
  id: 'sharepoint', name: 'SharePoint', category: 'Docs', tier: 'standard',
  status: 'connected', configured: true, metrics: [], lastSynced: '1 hour ago',
  reads: ['Sites', 'Libraries'], signalStrength: 55,
};

describe('SharePointSitePicker placement in ConnectorDetailPanel', () => {
  beforeEach(() => { vi.clearAllMocks(); mockUnconfigured(); });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('renders the site picker when SharePoint is connected', async () => {
    render(<ConnectorDetailPanel connector={sharepointConnector} onConfigure={vi.fn()} />);
    expect(await screen.findByText('Sites AgentIQ reads')).toBeInTheDocument();
  });

  it('does not render the site picker when SharePoint is not connected', () => {
    render(
      <ConnectorDetailPanel
        connector={{ ...sharepointConnector, status: 'not_connected', configured: false }}
        onConfigure={vi.fn()}
      />,
    );
    expect(screen.queryByText('Sites AgentIQ reads')).not.toBeInTheDocument();
  });
});
