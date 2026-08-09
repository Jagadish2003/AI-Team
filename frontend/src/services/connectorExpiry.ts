/**
 * Pre-launch connector token-expiry guard.
 *
 * A discovery run that uses a connector whose token has expired does NOT fail
 * loudly — it 401s per source mid-run, degrades that source to no data, and still
 * reports "Completed 100%". The user is left with a run that looks fine and is
 * quietly missing a system. So the expiry check has to happen BEFORE the run starts.
 *
 * This module is the single implementation of that check. It previously lived inline
 * in `StackBuilderPage`, which meant the OTHER launch entry point — the Discovery Run
 * page's `startRun` — was completely unguarded: launching from there started a live
 * run against an expired Salesforce token and only surfaced
 * `INVALID_SESSION_ID` in the server log. Extracting it here is what lets every
 * launch path share one guard instead of one path having it and the other not.
 *
 * Authority: `GET /api/connectors/{id}/token-status`, the same endpoint the
 * Integration Hub tile uses to decide whether to show "Reconnect" — so the guard and
 * the tile can never disagree about what "expired" means.
 */
import { fetchTokenStatus } from './staticApi';
import type { TokenStatus } from './staticApi';

/**
 * The two statuses that genuinely require the user to re-run the OAuth flow.
 *
 * Deliberately NOT `needs_refresh`: the vault silently mints a new access token from
 * the stored refresh token on next use, so treating it as expired would block runs
 * every time a short-lived access token lapsed (ServiceNow ~30 min, Salesforce ~1 h).
 *
 * `refresh_failed` matters as much as `needs_auth` here — it is the state the backend
 * sets when a live call was rejected 401 and the refresh could not recover, which is
 * exactly the Salesforce `INVALID_SESSION_ID` case where the token was revoked
 * server-side BEFORE its stored expiry, so a pure expiry check still reads "connected".
 */
export const RECONNECT_REQUIRED_STATUSES: readonly TokenStatus[] = [
  'needs_auth',
  'refresh_failed',
] as const;

export function needsReconnect(status: TokenStatus | null): boolean {
  return status !== null && RECONNECT_REQUIRED_STATUSES.includes(status);
}

/**
 * Narrow a run's systems to the ones worth checking: those the workspace has actually
 * engaged. A never-configured or unknown system is ignored — the run degrades
 * gracefully for those, and checking them would false-positive, because a
 * not-connected system legitimately reads `needs_auth`.
 */
export function connectorsToCheck(
  systems: string[],
  engagedConnectorIds: Iterable<string> | null | undefined,
): string[] {
  const engaged = new Set(engagedConnectorIds ?? []);
  return systems.filter((id) => engaged.has(id));
}

/** Given each checked connector's live status, the ones needing a reconnect. */
export function expiredFromStatuses(
  statuses: Array<{ id: string; status: TokenStatus | null }>,
): string[] {
  return statuses.filter((s) => needsReconnect(s.status)).map((s) => s.id);
}

/**
 * Read each connector's live token status and return those needing a reconnect.
 *
 * Per-connector failures are swallowed to `null` (treated as "not expired"): one
 * unreadable status must not block a launch, and the run would degrade that source
 * honestly anyway. The caller decides what to do with an empty result.
 */
export async function findExpiredConnectors(
  connectorIds: string[],
  fetchStatus: (id: string) => Promise<{ status: TokenStatus }> = (
    id: string,
  ) => fetchTokenStatus(id, { ensureValid: true }),
): Promise<string[]> {
  if (connectorIds.length === 0) return [];
  const statuses = await Promise.all(
    connectorIds.map(async (id) => {
      try {
        return { id, status: (await fetchStatus(id)).status };
      } catch {
        return { id, status: null as TokenStatus | null };
      }
    }),
  );
  return expiredFromStatuses(statuses);
}

/**
 * The user-facing message. One wording for every launch path, and it always names the
 * offending connectors and where to fix them — a toast saying only "something expired"
 * would leave the user guessing which of eight systems to reconnect.
 */
export function expiredConnectorMessage(
  displayNames: string[],
): string {
  const many = displayNames.length > 1;
  return (
    `Can't start discovery — ${many ? 'these connectors have' : 'this connector has'} ` +
    `an expired token: ${displayNames.join(', ')}. Reconnect ${many ? 'them' : 'it'} in the ` +
    `Integration Hub, then try again.`
  );
}

/**
 * The whole guard in one call: check the engaged subset of `systems` and return the
 * expired ids plus the ready-to-show message.
 *
 * Never throws — a failure of the check itself resolves to "nothing expired" so a
 * network blip cannot make the product unlaunchable.
 */
export async function checkConnectorExpiry(
  systems: string[],
  engagedConnectorIds: Iterable<string> | null | undefined,
  options: {
    displayName?: (id: string) => string;
    fetchStatus?: (id: string) => Promise<{ status: TokenStatus }>;
  } = {},
): Promise<{ expired: string[]; message: string | null }> {
  const toCheck = connectorsToCheck(systems, engagedConnectorIds);
  if (toCheck.length === 0) return { expired: [], message: null };
  try {
    const expired = await findExpiredConnectors(toCheck, options.fetchStatus);
    if (expired.length === 0) return { expired: [], message: null };
    const label = options.displayName ?? ((id: string) => id);
    return { expired, message: expiredConnectorMessage(expired.map(label)) };
  } catch {
    // The check itself failed — do not block the launch on it.
    return { expired: [], message: null };
  }
}
