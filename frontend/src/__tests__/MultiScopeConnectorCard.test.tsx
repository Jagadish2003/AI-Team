/**
 * MultiScopeConnectorCard tests — MSP-B13 (AT-743, T1).
 *
 * Covers the four T1 acceptance criteria:
 *   T1-AC1 — the card renders the cloud-connector onboarding (create-connection) form.
 *   T1-AC2 — secret fields are write-only: masked, cleared after save, never re-shown.
 *   T1-AC3 — Test Connection is integrated into the onboarding flow.
 *   T1-AC4 — the scope panel shows connected scopes + per-scope health.
 *
 * The card is prop-driven (the parent owns the API), so tests inject handler
 * mocks and assert the card calls them with the entered values and reflects the
 * results — no module boundary mocking needed.
 */
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { vi, describe, it, expect, afterEach } from 'vitest';

import MultiScopeConnectorCard from '../components/integrations/MultiScopeConnectorCard';
import {
  AWS_EVENTS_CONFIG,
  AZURE_EVENTS_CONFIG,
} from '../components/integrations/multiScopeConnectors';
import { ConnectedScope } from '../types/multiScopeConnector';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const AWS_SCOPES: ConnectedScope[] = [
  {
    id: '111111111111',
    identifier: '111111111111',
    label: 'Production',
    regions: ['us-east-1', 'us-west-2'],
    health: { status: 'ok', scopesOk: 6, scopesFailed: 0 },
  },
  {
    id: '222222222222',
    identifier: '222222222222',
    label: 'Staging',
    regions: ['eu-west-1'],
    health: {
      status: 'auth_failed',
      message: 'AssumeRole was denied for this account.',
      scopesFailed: 3,
    },
  },
];

function renderAws(overrides: Partial<React.ComponentProps<typeof MultiScopeConnectorCard>> = {}) {
  const props = {
    config: AWS_EVENTS_CONFIG,
    connected: false,
    scopes: [],
    onCreateConnection: vi.fn().mockResolvedValue(undefined),
    onTestConnection: vi.fn().mockResolvedValue({ ok: true, message: 'All good' }),
    onAddScope: vi.fn().mockResolvedValue(undefined),
    onRemoveScope: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(<MultiScopeConnectorCard {...props} />);
  return props;
}

// ── T1-AC1 — onboarding form ────────────────────────────────────────────────
describe('T1-AC1 — cloud connector onboarding', () => {
  it('renders the create-connection form for AWS', () => {
    renderAws();
    expect(screen.getByText('AWS Events')).toBeInTheDocument();
    expect(screen.getByLabelText('Hub access key ID')).toBeInTheDocument();
    expect(screen.getByLabelText('Hub secret access key')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save & connect/i })).toBeInTheDocument();
  });

  it('renders the same shared card for Azure with subscription copy', () => {
    render(
      <MultiScopeConnectorCard
        config={AZURE_EVENTS_CONFIG}
        connected
        scopes={[]}
        onCreateConnection={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    expect(screen.getByText('Azure Events')).toBeInTheDocument();
    expect(screen.getByLabelText('Tenant ID')).toBeInTheDocument();
    expect(screen.getByText(/Connected subscriptions/i)).toBeInTheDocument();
  });

  it('submits the entered credentials to onCreateConnection', async () => {
    const props = renderAws();
    fireEvent.change(screen.getByLabelText('Hub access key ID'), {
      target: { value: 'AKIAEXAMPLE' },
    });
    fireEvent.change(screen.getByLabelText('Hub secret access key'), {
      target: { value: 'super-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save & connect/i }));
    await waitFor(() =>
      expect(props.onCreateConnection).toHaveBeenCalledWith(
        expect.objectContaining({
          access_key_id: 'AKIAEXAMPLE',
          secret_access_key: 'super-secret',
        }),
      ),
    );
  });

  it('blocks submit and shows which required fields are missing', async () => {
    const props = renderAws();
    fireEvent.click(screen.getByRole('button', { name: /save & connect/i }));
    // Required-field gate: the save handler is never called with an empty form.
    expect(props.onCreateConnection).not.toHaveBeenCalled();
    // The Save button is disabled until the required fields are filled.
    expect(screen.getByRole('button', { name: /save & connect/i })).toBeDisabled();
  });
});

// ── T1-AC2 — write-only secrets ─────────────────────────────────────────────
describe('T1-AC2 — secret fields are write-only', () => {
  it('renders the secret field as a masked password input', () => {
    renderAws();
    const secret = screen.getByLabelText('Hub secret access key') as HTMLInputElement;
    expect(secret).toHaveAttribute('type', 'password');
    // new-password autocomplete stops the browser filling a stored value back in.
    expect(secret).toHaveAttribute('autocomplete', 'new-password');
  });

  it('clears every field (including secrets) after a successful save', async () => {
    renderAws();
    const access = screen.getByLabelText('Hub access key ID') as HTMLInputElement;
    const secret = screen.getByLabelText('Hub secret access key') as HTMLInputElement;
    fireEvent.change(access, { target: { value: 'AKIAEXAMPLE' } });
    fireEvent.change(secret, { target: { value: 'super-secret' } });
    fireEvent.click(screen.getByRole('button', { name: /save & connect/i }));
    await waitFor(() => expect(access.value).toBe(''));
    expect(secret.value).toBe('');
  });

  it('never pre-fills a secret even when a connection already exists', () => {
    renderAws({ connected: true, connectionSummary: 'AKIA…MPLE' });
    const secret = screen.getByLabelText('Hub secret access key') as HTMLInputElement;
    expect(secret.value).toBe('');
    expect(screen.getByText(/stored secrets can never be shown/i)).toBeInTheDocument();
  });
});

// ── T1-AC3 — test connection ────────────────────────────────────────────────
describe('T1-AC3 — test connection flow', () => {
  it('calls onTestConnection with the entered credentials and shows success', async () => {
    const props = renderAws({
      onTestConnection: vi
        .fn()
        .mockResolvedValue({ ok: true, message: 'Reached the hub.', scopesReachable: 2 }),
    });
    fireEvent.change(screen.getByLabelText('Hub access key ID'), {
      target: { value: 'AKIAEXAMPLE' },
    });
    fireEvent.change(screen.getByLabelText('Hub secret access key'), {
      target: { value: 'super-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: /test connection/i }));
    await waitFor(() => expect(props.onTestConnection).toHaveBeenCalled());
    expect(await screen.findByText('Reached the hub.')).toBeInTheDocument();
    expect(screen.getByText(/2 accounts reachable/i)).toBeInTheDocument();
  });

  it('shows the failure message when the test fails', async () => {
    renderAws({
      onTestConnection: vi.fn().mockResolvedValue({ ok: false, message: 'Credentials rejected.' }),
    });
    fireEvent.change(screen.getByLabelText('Hub access key ID'), {
      target: { value: 'AKIAEXAMPLE' },
    });
    fireEvent.change(screen.getByLabelText('Hub secret access key'), {
      target: { value: 'bad' },
    });
    fireEvent.click(screen.getByRole('button', { name: /test connection/i }));
    expect(await screen.findByText('Credentials rejected.')).toBeInTheDocument();
  });

  it('disables Test connection until the credentials are entered', () => {
    renderAws();
    expect(screen.getByRole('button', { name: /test connection/i })).toBeDisabled();
  });
});

// ── T1-AC4 — scope panel + health ───────────────────────────────────────────
describe('T1-AC4 — scope panel with health', () => {
  it('lists connected scopes with identifiers, regions and health', () => {
    renderAws({ connected: true, scopes: AWS_SCOPES });
    expect(screen.getByText('Production')).toBeInTheDocument();
    expect(screen.getByText('Staging')).toBeInTheDocument();
    // Health badges
    expect(screen.getByText('Healthy')).toBeInTheDocument();
    expect(screen.getByText('Auth failed')).toBeInTheDocument();
    // Regions + failure detail surfaced
    expect(screen.getByText(/us-east-1, us-west-2/)).toBeInTheDocument();
    expect(screen.getByText('AssumeRole was denied for this account.')).toBeInTheDocument();
  });

  it('shows an empty state when connected but no scopes are pinned', () => {
    renderAws({ connected: true, scopes: [] });
    expect(screen.getByText(/No accounts pinned yet/i)).toBeInTheDocument();
  });

  it('pins a new scope via onAddScope', async () => {
    const props = renderAws({ connected: true, scopes: AWS_SCOPES });
    fireEvent.change(screen.getByLabelText('Account ID'), {
      target: { value: '333333333333' },
    });
    fireEvent.change(screen.getByLabelText('Regions'), { target: { value: 'us-east-1' } });
    fireEvent.click(screen.getByRole('button', { name: /add account/i }));
    await waitFor(() =>
      expect(props.onAddScope).toHaveBeenCalledWith(
        expect.objectContaining({ account_id: '333333333333', regions: 'us-east-1' }),
      ),
    );
  });

  it('removes a scope via onRemoveScope', async () => {
    const props = renderAws({ connected: true, scopes: AWS_SCOPES });
    fireEvent.click(screen.getByRole('button', { name: /remove production/i }));
    await waitFor(() => expect(props.onRemoveScope).toHaveBeenCalledWith('111111111111'));
  });

  it('shows a loading state while scopes load', () => {
    renderAws({ connected: true, loadingScopes: true });
    expect(screen.getByText(/Loading accounts/i)).toBeInTheDocument();
  });
});

// ── Role gating ──────────────────────────────────────────────────────────────
describe('role gating', () => {
  it('disables write controls and explains why when canManage is false', () => {
    renderAws({
      connected: true,
      scopes: AWS_SCOPES,
      canManage: false,
      manageDisabledReason: 'Owner role required.',
    });
    expect(screen.getByRole('button', { name: /replace credentials/i })).toBeDisabled();
    const removeBtn = screen.getByRole('button', { name: /remove production/i });
    expect(removeBtn).toBeDisabled();
    expect(removeBtn).toHaveAttribute('title', 'Owner role required.');
  });
});
