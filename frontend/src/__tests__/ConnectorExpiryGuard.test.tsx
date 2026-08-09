/**
 * Pre-launch connector token-expiry guard — the shared implementation.
 *
 * A discovery run that uses an expired connector does not fail loudly: it 401s per
 * source mid-run, degrades that source to no data, and still reports
 * "Completed 100%". So the check must happen BEFORE the run starts.
 *
 * The guard existed, but only in `StackBuilderPage`. The Discovery Run page's
 * `startRun` had three call sites and none of them checked anything, so launching
 * from there against an expired Salesforce token started a live run whose only
 * symptom was `INVALID_SESSION_ID` in the server log. This suite covers the extracted
 * shared guard both paths now use.
 *
 * Two layers, deliberately:
 *   1. the decision logic (`services/connectorExpiry`), which is where all the
 *      behaviour lives and is pure/fast to test; and
 *   2. a SOURCE-level check that `DiscoveryRunPage` routes every start through it.
 *      The bug was not wrong logic — it was a call site bypassing the logic — so the
 *      regression worth pinning is "no unguarded start site exists", which a source
 *      assertion catches directly and cheaply.
 */
import { describe, expect, it, vi } from 'vitest';

import {
  RECONNECT_REQUIRED_STATUSES,
  checkConnectorExpiry,
  connectorsToCheck,
  expiredConnectorMessage,
  expiredFromStatuses,
  findExpiredConnectors,
  needsReconnect,
} from '../services/connectorExpiry';
import type { TokenStatus } from '../services/staticApi';

const status = (id: string, s: TokenStatus | null) => ({ id, status: s });

// ── Which statuses mean "the user must reconnect" ─────────────────────────────

describe('needsReconnect', () => {
  it('treats needs_auth as requiring a reconnect', () => {
    expect(needsReconnect('needs_auth')).toBe(true);
  });

  it('treats refresh_failed as requiring a reconnect', () => {
    // The Salesforce INVALID_SESSION_ID case: the token was revoked server-side
    // BEFORE its stored expiry, so a pure expiry check still reads "connected".
    expect(needsReconnect('refresh_failed')).toBe(true);
  });

  it('does NOT treat needs_refresh as expired', () => {
    // The vault silently mints a new access token from the stored refresh token, so
    // blocking here would stop runs every time a ~30-60 min access token lapsed.
    expect(needsReconnect('needs_refresh')).toBe(false);
  });

  it('does not treat a connected connector as expired', () => {
    expect(needsReconnect('connected')).toBe(false);
  });

  it('treats an unreadable status as not expired', () => {
    // One unreadable status must not block a launch.
    expect(needsReconnect(null)).toBe(false);
  });

  it('declares exactly the two reconnect-required statuses', () => {
    expect([...RECONNECT_REQUIRED_STATUSES]).toEqual(['needs_auth', 'refresh_failed']);
  });
});

// ── Which connectors are worth checking ──────────────────────────────────────

describe('connectorsToCheck', () => {
  it('checks only systems the workspace has actually engaged', () => {
    expect(connectorsToCheck(['salesforce', 'sap'], ['salesforce', 'jira'])).toEqual([
      'salesforce',
    ]);
  });

  it('ignores an unknown system rather than false-positiving on it', () => {
    // A never-configured system legitimately reads needs_auth; checking it would
    // block every launch that merely listed it.
    expect(connectorsToCheck(['sap'], ['salesforce'])).toEqual([]);
  });

  it('checks nothing when the engaged set is unavailable', () => {
    expect(connectorsToCheck(['salesforce'], null)).toEqual([]);
    expect(connectorsToCheck(['salesforce'], undefined)).toEqual([]);
  });

  it('preserves the caller order', () => {
    const engaged = ['a', 'b', 'c'];
    expect(connectorsToCheck(['c', 'a'], engaged)).toEqual(['c', 'a']);
  });
});

describe('expiredFromStatuses', () => {
  it('returns every connector needing a reconnect', () => {
    expect(
      expiredFromStatuses([
        status('salesforce', 'refresh_failed'),
        status('servicenow', 'needs_auth'),
        status('jira', 'connected'),
        status('slack', 'needs_refresh'),
        status('github', null),
      ]),
    ).toEqual(['salesforce', 'servicenow']);
  });

  it('returns nothing when every connector is healthy', () => {
    expect(
      expiredFromStatuses([status('jira', 'connected'), status('slack', 'needs_refresh')]),
    ).toEqual([]);
  });
});

// ── Reading live statuses ────────────────────────────────────────────────────

describe('findExpiredConnectors', () => {
  it('asks the backend to verify live validity before launch', async () => {
    const fetchMock = vi.fn(async (_url: string) =>
      new Response(JSON.stringify({ status: 'refresh_failed' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    try {
      expect(await findExpiredConnectors(['salesforce'])).toEqual(['salesforce']);
      expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
        '/api/connectors/salesforce/token-status?ensure_valid=true',
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('reads each connector and returns the expired ones', async () => {
    const fetchStatus = vi.fn(async (id: string) => ({
      status: (id === 'salesforce' ? 'refresh_failed' : 'connected') as TokenStatus,
    }));
    expect(await findExpiredConnectors(['salesforce', 'jira'], fetchStatus)).toEqual([
      'salesforce',
    ]);
    expect(fetchStatus).toHaveBeenCalledTimes(2);
  });

  it('makes no request when there is nothing to check', async () => {
    const fetchStatus = vi.fn();
    expect(await findExpiredConnectors([], fetchStatus)).toEqual([]);
    expect(fetchStatus).not.toHaveBeenCalled();
  });

  it('treats one connector whose status cannot be read as not expired', async () => {
    const fetchStatus = vi.fn(async (id: string) => {
      if (id === 'jira') throw new Error('network');
      return { status: 'connected' as TokenStatus };
    });
    // A single unreadable status must not block the launch…
    expect(await findExpiredConnectors(['salesforce', 'jira'], fetchStatus)).toEqual([]);
  });

  it('still reports the others when one read fails', async () => {
    // …but it must not mask a connector that IS expired.
    const fetchStatus = vi.fn(async (id: string) => {
      if (id === 'jira') throw new Error('network');
      return { status: 'needs_auth' as TokenStatus };
    });
    expect(await findExpiredConnectors(['salesforce', 'jira'], fetchStatus)).toEqual([
      'salesforce',
    ]);
  });
});

// ── The message the user actually sees ───────────────────────────────────────

describe('expiredConnectorMessage', () => {
  it('names the single offending connector and where to fix it', () => {
    const message = expiredConnectorMessage(['Salesforce CRM']);
    expect(message).toContain('Salesforce CRM');
    expect(message).toContain('Integration Hub');
    expect(message).toContain('this connector has');
  });

  it('pluralises and names every offender', () => {
    const message = expiredConnectorMessage(['Salesforce CRM', 'ServiceNow']);
    expect(message).toContain('Salesforce CRM, ServiceNow');
    expect(message).toContain('these connectors have');
    expect(message).toContain('Reconnect them');
  });

  it('never says only that something expired without naming it', () => {
    // A toast the user cannot act on is worse than none — with eight connected
    // systems, "a token expired" leaves them guessing which to reconnect.
    const message = expiredConnectorMessage(['Salesforce CRM']);
    expect(message).toMatch(/Salesforce CRM/);
  });
});

// ── The whole guard ─────────────────────────────────────────────────────────

describe('checkConnectorExpiry', () => {
  const healthy = async () => ({ status: 'connected' as TokenStatus });

  it('blocks with a message naming the expired connector', async () => {
    // The exact scenario from the bug report: a live run started against a
    // Salesforce token that had been revoked server-side.
    const result = await checkConnectorExpiry(
      ['salesforce', 'jira'],
      ['salesforce', 'jira'],
      {
        displayName: (id) => (id === 'salesforce' ? 'Salesforce CRM' : id),
        fetchStatus: async (id) => ({
          status: (id === 'salesforce' ? 'refresh_failed' : 'connected') as TokenStatus,
        }),
      },
    );
    expect(result.expired).toEqual(['salesforce']);
    expect(result.message).toContain('Salesforce CRM');
    expect(result.message).toContain('Integration Hub');
  });

  it('allows the launch when every connector is healthy', async () => {
    const result = await checkConnectorExpiry(['salesforce'], ['salesforce'], {
      fetchStatus: healthy,
    });
    expect(result.expired).toEqual([]);
    expect(result.message).toBeNull();
  });

  it('allows the launch when there is nothing to check', async () => {
    const fetchStatus = vi.fn();
    const result = await checkConnectorExpiry([], [], { fetchStatus });
    expect(result.message).toBeNull();
    expect(fetchStatus).not.toHaveBeenCalled();
  });

  it('does not block the launch when the check itself throws', async () => {
    // A network blip must not make the product unlaunchable.
    const result = await checkConnectorExpiry(['salesforce'], ['salesforce'], {
      fetchStatus: () => {
        throw new Error('boom');
      },
    });
    expect(result.message).toBeNull();
  });

  it('falls back to the raw id when no display name is supplied', async () => {
    const result = await checkConnectorExpiry(['salesforce'], ['salesforce'], {
      fetchStatus: async () => ({ status: 'needs_auth' as TokenStatus }),
    });
    expect(result.message).toContain('salesforce');
  });

  it('reports every expired connector, not just the first', async () => {
    const result = await checkConnectorExpiry(
      ['salesforce', 'servicenow', 'jira'],
      ['salesforce', 'servicenow', 'jira'],
      {
        fetchStatus: async (id) => ({
          status: (id === 'jira' ? 'connected' : 'needs_auth') as TokenStatus,
        }),
      },
    );
    expect(result.expired).toEqual(['salesforce', 'servicenow']);
  });
});

// ── No launch path may bypass the guard ──────────────────────────────────────

describe('DiscoveryRunPage routes every start through the guard', () => {
  // Read the page as raw text via Vite's import.meta.glob — the same idiom
  // R18C0_ComingSoonSweep uses, so no Node fs/types are needed in the
  // browser-mode Vitest environment.
  const pageSources = import.meta.glob('../pages/DiscoveryRunPage.tsx', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>;
  const rawSource = Object.values(pageSources)[0] ?? '';

  // Drop comment-only lines before counting call sites: the page's own comments
  // explain the bug and quote `startRun(inputs)`, which would otherwise be counted
  // as a call. Only whole-line comments are stripped, so a code line containing
  // `//` inside a string is left intact.
  const source = rawSource
    .split('\n')
    .filter((line: string) => {
      const t = line.trim();
      return !t.startsWith('//') && !t.startsWith('*') && !t.startsWith('/*');
    })
    .join('\n');

  it('found the page source', () => {
    // Guard on the guard: an empty read would make every assertion below vacuous.
    expect(rawSource.length).toBeGreaterThan(1000);
  });

  it('uses the shared expiry guard', () => {
    expect(source).toContain('checkConnectorExpiry');
  });

  it('defines a single guarded start wrapper', () => {
    expect(source).toContain('const startRunGuarded');
  });

  it('invokes the raw startRun exactly once — inside the guarded wrapper', () => {
    // THE regression. Three call sites previously invoked startRun(inputs)
    // directly, bypassing the check entirely. Exactly one invocation may remain:
    // the one inside startRunGuarded, after the expiry check. A new unguarded
    // site pushes this count above one and fails here.
    const raw = source.match(/\bstartRun\(inputs\)/g) ?? [];
    expect(raw).toHaveLength(1);

    const wrapperStart = source.indexOf('const startRunGuarded');
    expect(wrapperStart).toBeGreaterThan(-1);
    // The single invocation sits after the wrapper opens and after the check.
    expect(source.indexOf('startRun(inputs)')).toBeGreaterThan(wrapperStart);
    expect(source.indexOf('checkConnectorExpiry(')).toBeLessThan(
      source.indexOf('startRun(inputs)'),
    );
  });

  it('calls the guarded wrapper at every start site', () => {
    const guarded = source.match(/startRunGuarded\(\)/g) ?? [];
    // Auto-start effect, the "Start New Discovery Run" action, and Retry.
    expect(guarded.length).toBeGreaterThanOrEqual(3);
  });
});
