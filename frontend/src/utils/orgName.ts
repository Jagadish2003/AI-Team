/**
 * R17-D4 Addendum A §2 / T13 (AT-508) — client-side org-name fallback.
 *
 * The organisation display name is resolved ONCE server-side by T12
 * (`GET /api/license/org-name`, `app/org_display_name.py`): it returns the
 * license `org_name` (falling back to `customer` for pre-addendum keys) and a
 * neutral default before any key is installed — so `orgName` off the wire is
 * always a usable string (AC16). This helper only covers the brief client states
 * where that value is not yet available: the initial load before the fetch
 * resolves, and a transient fetch error. In both it substitutes the same neutral
 * default the server uses, so no surface ever renders a blank or placeholder name
 * (§5 "one name, resolved once").
 */

/**
 * Neutral default shown while the resolved name is loading or unavailable.
 * Kept identical to the server default (`DEFAULT_ORG_DISPLAY_NAME` in
 * `backend/app/org_display_name.py`) so the loading state never flickers a
 * different word than the resolved value.
 */
export const NEUTRAL_ORG_NAME = "Your Organisation";

/**
 * Resolve the organisation name to display. Returns the trimmed server-resolved
 * `orgName` when present, otherwise the neutral default. Accepts
 * `null`/`undefined`/blank (loading / transient error) and never returns an empty
 * string, so callers can render it directly.
 */
export function resolveOrgName(orgName?: string | null): string {
  const name = orgName?.trim();
  return name && name.length > 0 ? name : NEUTRAL_ORG_NAME;
}
