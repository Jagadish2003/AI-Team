// LIC-1 / T6 (AT-347) — admin license status shape.
// Mirrors the backend `LicenseStatusResponse` in backend/app/routes_license.py
// and the LicenseStatus values in backend/app/licensing.py.

export type LicenseStatusValue = "valid" | "grace" | "readonly" | "invalid";

export interface LicenseStatusResponse {
  /** Current license state. Drives the admin badge colour. */
  status: LicenseStatusValue;
  /** Issued-to customer name; null when there is no valid key. */
  customer: string | null;
  /** Term length in months (3 | 6 | 12); null when there is no valid key. */
  term: number | null;
  /** Term boundary, ISO date (YYYY-MM-DD); null when there is no valid key. */
  expires_at: string | null;
  /** Days until expiry (negative once expired); null when there is no valid key. */
  days_remaining: number | null;
}

/** Request body for POST /api/license/update-key. */
export interface UpdateLicenseKeyRequest {
  key: string;
}

/**
 * R17-D4 Addendum A / T10 (AT-505) — Integration-Hub license-limit state
 * (GET /api/license/limits). Systems used vs systems licensed, so the hub can
 * show current usage against the entitlement (AC14). Readable by any hub viewer
 * (viewer+), unlike the Owner-only full status above.
 *
 * The counts are the same ones the connect-time gate (T9) enforces, so the shown
 * count matches the enforced count — the "one connected entity = one system"
 * pricing definition.
 */
export interface LicenseLimitsResponse {
  /** Connected Integration-Hub entities for the org (one connected entity = one system). */
  systemsUsed: number;
  /** Licensed max systems; null for an unlimited/pre-addendum license. */
  systemsLicensed: number | null;
  /** True when no numeric cap applies (systemsLicensed is null). */
  unlimited: boolean;
  /**
   * Aggregate "is there headroom for a new system" signal (unlimited, or
   * used < licensed). Not a per-connector verdict — reconnecting an already
   * connected system is always allowed (forward-only), decided server-side.
   */
  canConnectMore: boolean;
}

/**
 * Minimal license signal for the global expiry banner (T9 / GET /api/license/banner).
 * Readable by any authenticated user (not just Owner), so the banner shows for
 * every role — including analysts whose discovery runs are blocked (AC4/AC5).
 */
export interface LicenseBannerResponse {
  status: LicenseStatusValue;
  /** Term boundary, ISO date (YYYY-MM-DD); null when there is no valid key. */
  expires_at: string | null;
  /**
   * Why the license is not valid, when applicable. Lets the banner distinguish a
   * never-licensed install (`no_license` / `signature_or_format` → "No valid
   * license installed") from an expired term (null → "License expired") and a
   * clock anomaly (`clock_rollback`). Null for valid/grace and past-grace expiry.
   */
  reason?: string | null;
  /**
   * Days left before a `grace` license crosses into read-only (discovery runs
   * blocked). Lets the grace banner say "runs blocked in N days" instead of a
   * bare "expired". Only populated in the `grace` state; null otherwise.
   */
  grace_days_remaining?: number | null;
}
