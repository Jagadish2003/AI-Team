/**
 * RunContext — org runs auto-select (workspace run visibility)
 *
 * Guards the fix for "the run I started isn't shown after logout / to another
 * member of my org." The run used to live only in one browser's localStorage;
 * now, when no run is selected, RunContext falls back to the org's most recent
 * run via GET /api/runs (org-scoped, newest-first). A cross-org/stale runId is
 * dropped and replaced by this org's latest, not left dangling.
 *
 * Run:
 *   npx vitest run src/__tests__/RunContextOrgRuns.test.tsx
 */

import '@testing-library/jest-dom/vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Control the in-session token RunContext reads.
let mockToken: string | null = 'jwt-token';
vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => (mockToken ? { token: mockToken } : { token: null }),
}));

import { RunProvider, useRunContext } from '../context/RunContext';

function RunIdProbe() {
  const { runId } = useRunContext();
  return <div data-testid="run-id">{runId ?? 'none'}</div>;
}

function renderProvider() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <RunProvider>
        <RunIdProbe />
      </RunProvider>
    </MemoryRouter>,
  );
}

describe('RunContext — org runs auto-select', () => {
  beforeEach(() => {
    mockToken = 'jwt-token';
    localStorage.clear();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it('auto-selects the org\'s most recent run when nothing is selected', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/api/runs')) {
        // Backend returns newest-first.
        return new Response(
          JSON.stringify([
            { id: 'run_newest', startedAt: '2026-06-10T10:00:00Z' },
            { id: 'run_older', startedAt: '2026-06-09T10:00:00Z' },
          ]),
          { status: 200, headers: { 'content-type': 'application/json' } },
        );
      }
      return new Response('null', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId('run-id')).toHaveTextContent('run_newest'),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/runs'),
      expect.anything(),
    );
  });

  it('falls back to the org latest when a stored runId is cross-org/stale (404)', async () => {
    localStorage.setItem('agentiq_run_id', 'run_fromAnotherOrg');

    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/api/runs/run_fromAnotherOrg')) {
        return new Response('null', { status: 404 }); // not in my org
      }
      if (url.endsWith('/api/runs')) {
        return new Response(
          JSON.stringify([{ id: 'run_mine', startedAt: '2026-06-10T10:00:00Z' }]),
          { status: 200, headers: { 'content-type': 'application/json' } },
        );
      }
      return new Response('null', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId('run-id')).toHaveTextContent('run_mine'),
    );
    expect(localStorage.getItem('agentiq_run_id')).toBe('run_mine');
  });

  it('selects nothing when the org has no runs', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/api/runs')) {
        return new Response('[]', {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response('null', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderProvider();

    await waitFor(() => expect(screen.getByTestId('run-id')).toHaveTextContent('none'));
  });

  it('does not fetch runs when logged out (token null)', async () => {
    mockToken = null;
    const fetchMock = vi.fn(async () => new Response('[]', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    renderProvider();

    await waitFor(() => expect(screen.getByTestId('run-id')).toHaveTextContent('none'));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
