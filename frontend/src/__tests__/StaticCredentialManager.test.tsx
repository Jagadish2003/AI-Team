/**
 * R17-D3 Addendum A (T12 / AC10) — Integration Hub static-credential flow.
 *
 * Option A layout: the right-panel StaticCredentialManager is READ-ONLY status;
 * all writes (enter / update / DELETE) happen in the modal opened from the tile's
 * "Set up outbound access" button — modelled here by passing an
 * `outboundSetupRequest` (owner-gated) which auto-opens the modal.
 *
 * Verifies:
 *   - Owner opens the entry modal via the setup request; a non-owner cannot.
 *   - Entering credentials POSTs URL + username + secret to the vault endpoint.
 *   - Values are WRITE-ONLY: the secret is never pre-filled, even when configured.
 *   - Validation + API errors surface inline; nothing is POSTed on invalid input.
 *   - Delete lives in the modal (confirm → DELETE).
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiDelete: vi.fn(),
  push: vi.fn(),
  role: { value: 'owner' as string | undefined },
}));

vi.mock('../lib/apiClient', () => ({
  ApiError: class ApiError extends Error {
    status: number;
    body: unknown;
    constructor(message: string, status: number, body: unknown) {
      super(message);
      this.status = status;
      this.body = body;
    }
  },
  apiGet: (...a: unknown[]) => mocks.apiGet(...a),
  apiPost: (...a: unknown[]) => mocks.apiPost(...a),
  apiDelete: (...a: unknown[]) => mocks.apiDelete(...a),
}));

vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: mocks.push }),
}));

vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({
    user: mocks.role.value ? { role: mocks.role.value } : null,
  }),
  useAuth: () => ({ user: mocks.role.value ? { role: mocks.role.value } : null }),
}));

import { ApiError } from '../lib/apiClient';
import StaticCredentialManager from '../components/integrations/StaticCredentialManager';
import type { Connector } from '../types/connector';
import type { OutboundSetupRequest } from '../types/connector';

const jira: Connector = {
  id: 'jira',
  name: 'Jira',
  category: 'Operational Systems',
  tier: 'recommended',
  status: 'not_configured',
  configured: false,
  metrics: [],
  lastSynced: '—',
  reads: [],
  signalStrength: 0,
} as Connector;

const NOT_CONFIGURED = {
  connector_id: 'jira',
  configured: false,
  base_url: null,
  has_username: false,
  updated_at: null,
};

const CONFIGURED = {
  connector_id: 'jira',
  configured: true,
  base_url: 'https://acme.atlassian.net',
  has_username: true,
  updated_at: '2026-07-01T10:00:00Z',
};

// Auto-open the modal the way the tile's "Set up outbound access" button does.
const OPEN_REQUEST: OutboundSetupRequest = { connectorId: 'jira', nonce: 1 };

function renderManager(request: OutboundSetupRequest | null = null) {
  return render(<StaticCredentialManager connector={jira} outboundSetupRequest={request} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.role.value = 'owner';
  mocks.apiGet.mockResolvedValue(NOT_CONFIGURED);
  mocks.apiPost.mockResolvedValue(CONFIGURED);
  mocks.apiDelete.mockResolvedValue(undefined);
});

describe('StaticCredentialManager — read-only status panel', () => {
  it('fetches non-secret status from the credentials endpoint', async () => {
    renderManager();
    await waitFor(() =>
      expect(mocks.apiGet).toHaveBeenCalledWith('/api/connectors/jira/credentials'),
    );
  });

  it('shows no write controls in the panel (writes live in the tile modal)', async () => {
    renderManager();
    await screen.findByText(/use "set up outbound access"/i);
    expect(screen.queryByRole('button', { name: /enter credentials/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /update credentials/i })).not.toBeInTheDocument();
  });
});

describe('StaticCredentialManager — Owner gating (AC10)', () => {
  it('opens the entry modal for an Owner via the setup request', async () => {
    renderManager(OPEN_REQUEST);
    expect(await screen.findByRole('dialog', { name: /jira credentials/i })).toBeInTheDocument();
  });

  it('does not open the modal for a non-owner, and shows a note', async () => {
    mocks.role.value = 'analyst';
    renderManager(OPEN_REQUEST);
    expect(await screen.findByText(/only workspace owners can manage/i)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('StaticCredentialManager — entry POSTs to the vault (AC10)', () => {
  it('submits URL + username + secret to the credentials endpoint', async () => {
    renderManager(OPEN_REQUEST);
    await screen.findByRole('dialog', { name: /jira credentials/i });

    fireEvent.change(screen.getByLabelText('Jira base URL'), {
      target: { value: 'https://acme.atlassian.net' },
    });
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'svc@acme.com' } });
    fireEvent.change(screen.getByLabelText('API token'), { target: { value: 'FAKE-token-123' } });
    fireEvent.click(screen.getByRole('button', { name: /save credential/i }));

    await waitFor(() =>
      expect(mocks.apiPost).toHaveBeenCalledWith('/api/connectors/jira/credentials', {
        base_url: 'https://acme.atlassian.net',
        username: 'svc@acme.com',
        secret: 'FAKE-token-123',
      }),
    );
    expect(mocks.push).toHaveBeenCalledWith(expect.stringMatching(/saved/i), 'success');
    // The modal must close after a successful save.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('does not POST when a field is missing, and shows an inline error', async () => {
    renderManager(OPEN_REQUEST);
    await screen.findByRole('dialog', { name: /jira credentials/i });

    fireEvent.change(screen.getByLabelText('Jira base URL'), {
      target: { value: 'https://acme.atlassian.net' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save credential/i }));

    expect(await screen.findByText(/enter the .*jira base url/i)).toBeInTheDocument();
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('surfaces a backend error inline (e.g. vault not configured)', async () => {
    mocks.apiPost.mockRejectedValueOnce(
      new ApiError('fail', 500, { detail: 'Credential vault is not configured.' }),
    );
    renderManager(OPEN_REQUEST);
    await screen.findByRole('dialog', { name: /jira credentials/i });

    fireEvent.change(screen.getByLabelText('Jira base URL'), {
      target: { value: 'https://acme.atlassian.net' },
    });
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'svc@acme.com' } });
    fireEvent.change(screen.getByLabelText('API token'), { target: { value: 'FAKE-x' } });
    fireEvent.click(screen.getByRole('button', { name: /save credential/i }));

    expect(await screen.findByText(/credential vault is not configured/i)).toBeInTheDocument();
  });
});

describe('StaticCredentialManager — write-only (AC10)', () => {
  it('never pre-fills the secret even when a credential is already configured', async () => {
    mocks.apiGet.mockResolvedValue(CONFIGURED);
    renderManager(OPEN_REQUEST);

    // Once status resolves the modal reflects "configured": base_url pre-filled,
    // secret always blank.
    await waitFor(() =>
      expect(screen.getByLabelText('Jira base URL')).toHaveValue('https://acme.atlassian.net'),
    );
    expect(screen.getByLabelText('API token')).toHaveValue('');
  });
});

describe('StaticCredentialManager — delete lives in the modal', () => {
  it('requires a confirm click, then DELETEs the credential', async () => {
    mocks.apiGet.mockResolvedValue(CONFIGURED);
    renderManager(OPEN_REQUEST);

    const remove = await screen.findByRole('button', { name: /^remove$/i });
    fireEvent.click(remove);
    // First click asks for confirmation — no DELETE yet.
    expect(mocks.apiDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /click to confirm/i }));

    await waitFor(() =>
      expect(mocks.apiDelete).toHaveBeenCalledWith('/api/connectors/jira/credentials'),
    );
  });
});
