/**
 * CS-2 / AT-323 (T1) — connectConnectorApi triggers real OAuth.
 *
 * Guards the fix that replaced the fake POST /api/connectors/{id}/connect
 * (which marked a connector connected without any OAuth) with the real OAuth
 * initiation flow: GET /api/connectors/{id}/auth-url, then a browser redirect
 * to the returned provider login URL.
 *
 * Run:
 *   npx vitest run src/__tests__/connectConnectorApi.test.ts
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { connectConnectorApi } from '../services/staticApi';

describe('connectConnectorApi — real OAuth Connect (AT-323/T1)', () => {
  const realLocation = window.location;

  beforeEach(() => {
    // window.location is read-only; replace with a writable stub so we can
    // observe the redirect (window.location.href = auth_url).
    delete (window as any).location;
    (window as any).location = { href: '' };
  });

  afterEach(() => {
    (window as any).location = realLocation;
    vi.restoreAllMocks();
  });

  it('fetches the auth URL (GET) and redirects the browser to it', async () => {
    const authUrl =
      'https://login.salesforce.com/services/oauth2/authorize?state=nonce';
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ({ auth_url: authUrl, connector_id: 'salesforce' }),
      text: async () => '',
    })) as unknown as typeof fetch;
    vi.stubGlobal('fetch', fetchMock);

    await connectConnectorApi('salesforce');

    // T1-AC1: calls GET /api/connectors/{id}/auth-url
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = (fetchMock as any).mock.calls[0];
    expect(String(calledUrl)).toContain('/api/connectors/salesforce/auth-url');
    // GET (no explicit method on apiGet) and never the old POST /connect body
    expect(init?.method ?? 'GET').toBe('GET');
    // T1-AC3: the old POST /api/connectors/{id}/connect is no longer called
    expect(String(calledUrl).endsWith('/connect')).toBe(false);
    expect(String(calledUrl)).toMatch(/\/auth-url$/);

    // T1-AC2 / T1-AC4: browser is redirected to the returned auth_url
    expect(window.location.href).toBe(authUrl);
  });

  it('rejects (does not redirect) when fetching the auth URL fails', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 500,
      headers: { get: () => 'application/json' },
      json: async () => ({ detail: 'boom' }),
      text: async () => 'boom',
    })) as unknown as typeof fetch;
    vi.stubGlobal('fetch', fetchMock);

    await expect(connectConnectorApi('servicenow')).rejects.toThrow();
    expect(window.location.href).toBe('');
  });
});
