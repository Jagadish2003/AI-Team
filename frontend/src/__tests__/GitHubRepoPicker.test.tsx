/**
 * GitHubRepoPicker — multi-repo selection tests.
 *
 * Run: npx vitest run src/__tests__/GitHubRepoPicker.test.tsx
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

import GitHubRepoPicker from '../components/integrations/GitHubRepoPicker';
import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';
import { Connector } from '../types/connector';

const AVAILABLE = [
  { id: 'acme/web-app', name: 'web-app', owner: 'acme' },
  { id: 'acme/api', name: 'api', owner: 'acme' },
];

function mockUnconfigured() {
  mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: [], configured: false });
  mockApiPatch.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['acme/api'], configured: true });
}

describe('GitHubRepoPicker', () => {
  beforeEach(() => { vi.clearAllMocks(); mockUnconfigured(); });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('lists the selectable repositories (name + owner/repo)', async () => {
    render(<GitHubRepoPicker />);
    expect(await screen.findByText('web-app')).toBeInTheDocument();
    expect(screen.getByText('acme/web-app')).toBeInTheDocument();
    expect(screen.getByText('api')).toBeInTheDocument();
  });

  it('multi-select: saving PATCHes all chosen repo ids', async () => {
    render(<GitHubRepoPicker />);
    fireEvent.click(await screen.findByText('web-app'));
    fireEvent.click(screen.getByText('api'));
    fireEvent.click(screen.getByText('Save repository selection'));
    await waitFor(() => expect(mockApiPatch).toHaveBeenCalledTimes(1));
    expect(mockApiPatch).toHaveBeenCalledWith('/api/connectors/github/repos', {
      repos: ['acme/web-app', 'acme/api'],
    });
  });

  it('honours a previously-saved selection on load', async () => {
    mockApiGet.mockResolvedValue({ ok: true, available: AVAILABLE, selected: ['acme/api'], configured: true });
    render(<GitHubRepoPicker />);
    const api = await screen.findByRole('checkbox', { name: /acme\/api/ });
    expect(api).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('1 of 2 repositories selected')).toBeInTheDocument();
  });
});

const githubConnector: Connector = {
  id: 'github', name: 'GitHub', category: 'Engineering', tier: 'standard',
  status: 'connected', configured: true, metrics: [], lastSynced: '1 hour ago',
  reads: ['Repos', 'PRs', 'Commits'], signalStrength: 70,
};

describe('GitHubRepoPicker placement in ConnectorDetailPanel', () => {
  beforeEach(() => { vi.clearAllMocks(); mockUnconfigured(); });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('renders the repo picker when GitHub is connected', async () => {
    render(<ConnectorDetailPanel connector={githubConnector} onConfigure={vi.fn()} />);
    expect(await screen.findByText('Repositories AgentIQ reads')).toBeInTheDocument();
  });

  it('does not render the repo picker when GitHub is not connected', () => {
    render(
      <ConnectorDetailPanel
        connector={{ ...githubConnector, status: 'not_connected', configured: false }}
        onConfigure={vi.fn()}
      />,
    );
    expect(screen.queryByText('Repositories AgentIQ reads')).not.toBeInTheDocument();
  });
});
