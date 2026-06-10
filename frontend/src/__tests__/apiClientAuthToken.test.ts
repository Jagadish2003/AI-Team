/**
 * apiClient — in-session JWT wiring (multi-tenancy)
 *
 * Guards the fix for cross-org workspace leakage: every data request must be
 * signed with the LOGGED-IN user's JWT, not the static dev token. Signing with
 * the dev token (no org claim) made every user resolve to the `default` org, so
 * org B saw org A's connectors and runs.
 *
 * Run:
 *   npx vitest run src/__tests__/apiClientAuthToken.test.ts
 */

import { afterEach, describe, expect, it } from 'vitest';
import { authHeader, getAuthToken, setAuthToken } from '../lib/apiClient';

const DEV_FALLBACK =
  (import.meta.env.VITE_DEV_JWT as string | undefined) ?? 'dev-token-change-me';

describe('apiClient authHeader — in-session JWT', () => {
  afterEach(() => setAuthToken(null));

  it('signs with the in-session JWT when a user is logged in', () => {
    setAuthToken('jwt-for-org-b');
    const h = authHeader();
    expect(h.Authorization).toBe('Bearer jwt-for-org-b');
    // org_id is carried in the JWT claim — X-Org-Id must NOT be attached
    // (a mismatched value would trip the backend 403 impersonation guard).
    expect(h['X-Org-Id']).toBeUndefined();
    expect(getAuthToken()).toBe('jwt-for-org-b');
  });

  it('falls back to the dev token when logged out (no session token)', () => {
    setAuthToken(null);
    expect(authHeader().Authorization).toBe(`Bearer ${DEV_FALLBACK}`);
    expect(getAuthToken()).toBeNull();
  });

  it('switches token when a different user logs in (no cross-org bleed)', () => {
    setAuthToken('user-a-org-a');
    expect(authHeader().Authorization).toBe('Bearer user-a-org-a');
    setAuthToken('user-b-org-b');
    expect(authHeader().Authorization).toBe('Bearer user-b-org-b');
  });
});
