/**
 * MultiScopeConnectorManager tests — MSP-B13 (AT-744, T2).
 *
 * The manager wires the shared card to the Cloud Connector Onboarding backend.
 * These tests mock the service boundary (`services/cloudConnectorApi`) and assert
 * the manager calls the right endpoint with the right body for each provider:
 *   - AWS create sends the selected partition; add-scope splits regions to a list.
 *   - Azure create sends environment + mode; a candidate pins by subscription_id.
 *   - Test connection surfaces the validated identity.
 *   - Owner-only: writes are disabled for a non-Owner.
 */
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { vi, describe, it, expect, afterEach, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  createCloudConnection: vi.fn(),
  testCloudConnection: vi.fn(),
  fetchCloudScopes: vi.fn(),
  pinCloudScope: vi.fn(),
  unpinCloudScope: vi.fn(),
  push: vi.fn(),
  invalidate: vi.fn(),
  role: 'owner' as 'owner' | 'analyst' | 'viewer',
}));

vi.mock('../services/cloudConnectorApi', () => ({
  createCloudConnection: (...a: unknown[]) => mocks.createCloudConnection(...a),
  testCloudConnection: (...a: unknown[]) => mocks.testCloudConnection(...a),
  fetchCloudScopes: (...a: unknown[]) => mocks.fetchCloudScopes(...a),
  pinCloudScope: (...a: unknown[]) => mocks.pinCloudScope(...a),
  unpinCloudScope: (...a: unknown[]) => mocks.unpinCloudScope(...a),
}));

vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({ user: { email: 'o@x.com', role: mocks.role } }),
}));

vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: mocks.push }),
}));

vi.mock('../lib/dataCache', () => ({
  useDataCache: () => ({ invalidate: mocks.invalidate }),
}));

import MultiScopeConnectorManager from '../components/integrations/MultiScopeConnectorManager';

const EMPTY_SCOPES = { connector_id: '', provider: '', scopes: [], candidates: [] };

function connector(id: string, status = 'not_configured') {
  return {
    id,
    name: id === 'aws_events' ? 'AWS Events' : 'Azure Events',
    category: 'Cloud Operations',
    tier: 'standard' as const,
    status: status as 'connected' | 'not_configured',
    configured: status === 'connected',
    metrics: [],
    lastSynced: '—',
    reads: [],
    signalStrength: 55,
  };
}

beforeEach(() => {
  mocks.role = 'owner';
  mocks.fetchCloudScopes.mockResolvedValue({ ...EMPTY_SCOPES });
  mocks.createCloudConnection.mockResolvedValue({
    connector_id: 'x',
    provider: 'x',
    configured: true,
    status: 'connected',
    scope_count: 0,
  });
  mocks.pinCloudScope.mockResolvedValue({ ...EMPTY_SCOPES });
  mocks.unpinCloudScope.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('MultiScopeConnectorManager — scope loading', () => {
  it('loads scopes for the connector on mount', async () => {
    render(<MultiScopeConnectorManager connector={connector('aws_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalledWith('aws_events'));
  });

  it('renders Azure discovered subscriptions as candidates (T2-AC3)', async () => {
    mocks.fetchCloudScopes.mockResolvedValue({
      ...EMPTY_SCOPES,
      candidates: ['sub-777'],
    });
    render(<MultiScopeConnectorManager connector={connector('azure_events', 'connected') as any} />);
    expect(await screen.findByText('sub-777')).toBeInTheDocument();
  });
});

describe('MultiScopeConnectorManager — AWS wiring (T2-AC1)', () => {
  it('creates the AWS connection with the selected partition', async () => {
    render(<MultiScopeConnectorManager connector={connector('aws_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('Partition'), { target: { value: 'aws-us-gov' } });
    fireEvent.change(screen.getByLabelText('Hub access key ID'), {
      target: { value: 'AKIA' },
    });
    fireEvent.change(screen.getByLabelText('Hub secret access key'), {
      target: { value: 'shh' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save & connect/i }));
    await waitFor(() =>
      expect(mocks.createCloudConnection).toHaveBeenCalledWith(
        'aws_events',
        expect.objectContaining({ partition: 'aws-us-gov', access_key_id: 'AKIA' }),
      ),
    );
  });

  it('splits regions into a list when pinning an AWS account', async () => {
    render(
      <MultiScopeConnectorManager connector={connector('aws_events', 'connected') as any} />,
    );
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('Account ID'), { target: { value: '999999999999' } });
    fireEvent.change(screen.getByLabelText(/role ARN/i), {
      target: { value: 'arn:aws:iam::999999999999:role/RO' },
    });
    fireEvent.change(screen.getByLabelText('Regions'), {
      target: { value: 'us-east-1, us-west-2' },
    });
    fireEvent.click(screen.getByRole('button', { name: /add account/i }));
    await waitFor(() =>
      expect(mocks.pinCloudScope).toHaveBeenCalledWith(
        'aws_events',
        expect.objectContaining({
          account_id: '999999999999',
          role_arn: 'arn:aws:iam::999999999999:role/RO',
          regions: ['us-east-1', 'us-west-2'],
        }),
      ),
    );
  });
});

describe('MultiScopeConnectorManager — Azure wiring (T2-AC2/AC4)', () => {
  it('creates the Azure connection with environment + mode', async () => {
    render(<MultiScopeConnectorManager connector={connector('azure_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('Tenant ID'), { target: { value: 'tid' } });
    fireEvent.change(screen.getByLabelText('Client (application) ID'), {
      target: { value: 'cid' },
    });
    fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 'sec' } });
    fireEvent.click(screen.getByRole('button', { name: /save & connect/i }));
    await waitFor(() =>
      expect(mocks.createCloudConnection).toHaveBeenCalledWith(
        'azure_events',
        expect.objectContaining({
          environment: 'AzureCloud',
          mode: 'direct',
          tenant_id: 'tid',
          client_id: 'cid',
          client_secret: 'sec',
        }),
      ),
    );
  });

  it('pins a candidate subscription by subscription_id (T2-AC4)', async () => {
    mocks.fetchCloudScopes.mockResolvedValue({ ...EMPTY_SCOPES, candidates: ['sub-42'] });
    render(<MultiScopeConnectorManager connector={connector('azure_events', 'connected') as any} />);
    const pinBtn = await screen.findByRole('button', { name: /pin sub-42/i });
    fireEvent.click(pinBtn);
    await waitFor(() =>
      expect(mocks.pinCloudScope).toHaveBeenCalledWith('azure_events', {
        subscription_id: 'sub-42',
      }),
    );
  });
});

describe('MultiScopeConnectorManager — test connection + gating', () => {
  it('surfaces the validated identity from a successful test', async () => {
    mocks.testCloudConnection.mockResolvedValue({
      connector_id: 'aws_events',
      provider: 'aws',
      ok: true,
      message: 'Connection validated.',
      identity: '123456789012',
    });
    render(<MultiScopeConnectorManager connector={connector('aws_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('Hub access key ID'), { target: { value: 'AKIA' } });
    fireEvent.change(screen.getByLabelText('Hub secret access key'), { target: { value: 'shh' } });
    fireEvent.click(screen.getByRole('button', { name: /test connection/i }));
    expect(await screen.findByText(/Connection validated \(123456789012\)/)).toBeInTheDocument();
  });

  it('disables write controls for a non-owner', async () => {
    mocks.role = 'viewer';
    render(<MultiScopeConnectorManager connector={connector('aws_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    expect(screen.getByRole('button', { name: /save & connect/i })).toBeDisabled();
  });
});

// ── T6-AC2 / AC3 — RBAC: Owner manages; Analyst/Viewer read-only health ─────
describe('MultiScopeConnectorManager — RBAC (T6-AC2/AC3)', () => {
  it.each(['analyst', 'viewer'] as const)(
    'shows a read-only notice and lets %s view health but not modify',
    async (role) => {
      mocks.role = role;
      mocks.fetchCloudScopes.mockResolvedValue({
        ...EMPTY_SCOPES,
        scopes: [
          {
            scope_id: '123456789012',
            kind: 'aws_account',
            label: 'Prod',
            status: 'ok',
            regions: ['us-east-1'],
          },
        ],
      });
      render(<MultiScopeConnectorManager connector={connector('aws_events', 'connected') as any} />);
      // Health is visible (read).
      expect(await screen.findByText('Prod')).toBeInTheDocument();
      expect(screen.getByText('Healthy')).toBeInTheDocument();
      // Read-only affordance + disabled write control (no modify).
      expect(screen.getByTestId('cloud-connector-readonly')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /replace credentials/i })).toBeDisabled();
    },
  );

  it('lets an Owner manage (write controls enabled, no read-only notice)', async () => {
    mocks.role = 'owner';
    render(<MultiScopeConnectorManager connector={connector('aws_events', 'connected') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    expect(screen.queryByTestId('cloud-connector-readonly')).not.toBeInTheDocument();
    // Save enabled once credentials are entered.
    fireEvent.change(screen.getByLabelText('Hub access key ID'), { target: { value: 'AKIA' } });
    fireEvent.change(screen.getByLabelText('Hub secret access key'), { target: { value: 'shh' } });
    expect(screen.getByRole('button', { name: /replace credentials/i })).toBeEnabled();
  });
});

// ── Dynamic connection status (badge driven by backend state) ───────────────
describe('MultiScopeConnectorManager — dynamic connection status', () => {
  it('shows Not Connected before any credentials are configured', async () => {
    render(<MultiScopeConnectorManager connector={connector('aws_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    expect(screen.getByText('Not connected')).toBeInTheDocument();
    // Never a premature green "Connected" badge.
    expect(screen.queryByText('Connected')).not.toBeInTheDocument();
  });

  it('does NOT infer Connected from pre-existing scopes when the connector is not configured', async () => {
    // Regression guard: pinned scopes are data, not a connection signal.
    mocks.fetchCloudScopes.mockResolvedValue({
      ...EMPTY_SCOPES,
      scopes: [{ scope_id: '111111111111', kind: 'aws_account', status: 'ok' }],
    });
    render(<MultiScopeConnectorManager connector={connector('aws_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.queryByText('Connected')).not.toBeInTheDocument();
  });

  it('shows a Connecting… state while the create request is in flight, then Connected', async () => {
    let resolveCreate: (v: unknown) => void = () => {};
    mocks.createCloudConnection.mockReturnValue(
      new Promise((res) => {
        resolveCreate = res;
      }),
    );
    render(<MultiScopeConnectorManager connector={connector('aws_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('Hub access key ID'), { target: { value: 'AKIA' } });
    fireEvent.change(screen.getByLabelText('Hub secret access key'), { target: { value: 'shh' } });
    fireEvent.click(screen.getByRole('button', { name: /save & connect/i }));

    // In-flight: connecting badge + disabled button (no repeat submissions), no green yet.
    // "Connecting…" appears in both the header badge and the submit button.
    expect((await screen.findAllByText(/Connecting…/)).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText('Connected')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /connecting…/i })).toBeDisabled();

    // Resolve → backend confirmed → green Connected.
    resolveCreate({ connector_id: 'aws_events', provider: 'aws', configured: true, status: 'connected', scope_count: 0 });
    expect(await screen.findByText('Connected')).toBeInTheDocument();
  });

  it('stays Not Connected and surfaces the backend error when create fails', async () => {
    const { ApiError } = await import('../lib/apiClient');
    mocks.createCloudConnection.mockRejectedValue(
      new ApiError('bad', 400, { detail: 'AWS rejected the credentials.' }),
    );
    render(<MultiScopeConnectorManager connector={connector('aws_events') as any} />);
    await waitFor(() => expect(mocks.fetchCloudScopes).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('Hub access key ID'), { target: { value: 'AKIA' } });
    fireEvent.change(screen.getByLabelText('Hub secret access key'), { target: { value: 'bad' } });
    fireEvent.click(screen.getByRole('button', { name: /save & connect/i }));

    expect(await screen.findByText('AWS rejected the credentials.')).toBeInTheDocument();
    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.queryByText('Connected')).not.toBeInTheDocument();
  });

  it('renders the provider-branded icon (not the generic fallback)', async () => {
    const { connectorIcons } = await import('../components/integrations/ConnectorIcons');
    expect(connectorIcons['AWS Events']).toBeTruthy();
    expect(connectorIcons['Azure Events']).toBeTruthy();
  });
});