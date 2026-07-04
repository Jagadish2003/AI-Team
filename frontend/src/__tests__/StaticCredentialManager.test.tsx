/**
 * R17-D3 Addendum A (T12 / AC10) — Integration Hub static-credential form tests.
 *
 * Verifies the frontend half of AC10:
 *   - Only an Owner can manage credentials (non-owners see a note, no controls).
 *   - Entering credentials POSTs URL + username + secret to the vault endpoint.
 *   - Values are WRITE-ONLY: the secret field is never pre-filled from status,
 *     even when a credential is already configured.
 *   - Validation and API errors surface inline; nothing is POSTed on invalid input.
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

async function openModal() {
  fireEvent.click(await screen.findByRole('button', { name: /credentials/i }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.role.value = 'owner';
  mocks.apiGet.mockResolvedValue(NOT_CONFIGURED);
  mocks.apiPost.mockResolvedValue(CONFIGURED);
  mocks.apiDelete.mockResolvedValue(undefined);
});

describe('StaticCredentialManager — Owner gating (AC10)', () => {
  it('fetches non-secret status from the credentials endpoint', async () => {
    render(<StaticCredentialManager connector={jira} />);
    await waitFor(() =>
      expect(mocks.apiGet).toHaveBeenCalledWith('/api/connectors/jira/credentials'),
    );
  });

  it('shows the Enter credentials control to an Owner', async () => {
    render(<StaticCredentialManager connector={jira} />);
    expect(
      await screen.findByRole('button', { name: /enter credentials/i }),
    ).toBeInTheDocument();
  });

  it('hides the controls and shows a note for a non-owner', async () => {
    mocks.role.value = 'analyst';
    render(<StaticCredentialManager connector={jira} />);
    expect(
      await screen.findByText(/only workspace owners can manage/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /enter credentials/i }),
    ).not.toBeInTheDocument();
  });
});

describe('StaticCredentialManager — entry POSTs to the vault (AC10)', () => {
  it('submits URL + username + secret to the credentials endpoint', async () => {
    render(<StaticCredentialManager connector={jira} />);
    await openModal();

    fireEvent.change(screen.getByLabelText('Jira base URL'), {
      target: { value: 'https://acme.atlassian.net' },
    });
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'svc@acme.com' },
    });
    fireEvent.change(screen.getByLabelText('API token'), {
      target: { value: 'FAKE-token-123' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save credential/i }));

    await waitFor(() =>
      expect(mocks.apiPost).toHaveBeenCalledWith('/api/connectors/jira/credentials', {
        base_url: 'https://acme.atlassian.net',
        username: 'svc@acme.com',
        secret: 'FAKE-token-123',
      }),
    );
    expect(mocks.push).toHaveBeenCalledWith(expect.stringMatching(/saved/i), 'success');
  });

  it('does not POST when a field is missing, and shows an inline error', async () => {
    render(<StaticCredentialManager connector={jira} />);
    await openModal();

    fireEvent.change(screen.getByLabelText('Jira base URL'), {
      target: { value: 'https://acme.atlassian.net' },
    });
    // Leave username + secret blank.
    fireEvent.click(screen.getByRole('button', { name: /save credential/i }));

    expect(await screen.findByText(/enter the .*jira base url/i)).toBeInTheDocument();
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('surfaces a backend error inline (e.g. vault not configured)', async () => {
    mocks.apiPost.mockRejectedValueOnce(
      new ApiError('fail', 500, { detail: 'Credential vault is not configured.' }),
    );
    render(<StaticCredentialManager connector={jira} />);
    await openModal();

    fireEvent.change(screen.getByLabelText('Jira base URL'), {
      target: { value: 'https://acme.atlassian.net' },
    });
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'svc@acme.com' } });
    fireEvent.change(screen.getByLabelText('API token'), { target: { value: 'FAKE-x' } });
    fireEvent.click(screen.getByRole('button', { name: /save credential/i }));

    expect(
      await screen.findByText(/credential vault is not configured/i),
    ).toBeInTheDocument();
  });
});

describe('StaticCredentialManager — write-only (AC10)', () => {
  it('never pre-fills the secret even when a credential is already configured', async () => {
    mocks.apiGet.mockResolvedValue(CONFIGURED);
    render(<StaticCredentialManager connector={jira} />);

    // Configured status is shown, and the control offers to REPLACE.
    expect(await screen.findByText(/credentials configured/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /update credentials/i }));

    // The non-secret base_url may be pre-filled; the secret must be blank.
    expect(screen.getByLabelText('Jira base URL')).toHaveValue('https://acme.atlassian.net');
    expect(screen.getByLabelText('API token')).toHaveValue('');
  });
});

describe('StaticCredentialManager — remove (Owner-only)', () => {
  it('requires a confirm click, then DELETEs the credential', async () => {
    mocks.apiGet.mockResolvedValue(CONFIGURED);
    render(<StaticCredentialManager connector={jira} />);

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
