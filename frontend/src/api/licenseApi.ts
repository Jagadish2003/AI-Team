/**
 * LIC-1 / T8 (AT-349) — admin license API wrapper.
 *
 * Typed client over the T6 (AT-347) Owner-only endpoints:
 *   GET  /api/license             → current status + details
 *   POST /api/license/update-key  → validate a pasted key, store if valid,
 *                                    return refreshed status (400 if invalid).
 *
 * Goes through the shared apiClient so requests carry the in-session JWT and
 * never hardcode a host outside the dev fallback. Callers handle ApiError.
 */
import { apiGet, apiPost } from "../lib/apiClient";
import type {
  LicenseBannerResponse,
  LicenseLimitsResponse,
  LicenseOrgNameResponse,
  LicenseStatusResponse,
  UpdateLicenseKeyRequest,
} from "../types/license";

export type {
  LicenseBannerResponse,
  LicenseLimitsResponse,
  LicenseOrgNameResponse,
  LicenseStatusResponse,
  UpdateLicenseKeyRequest,
};

/** GET /api/license — full current license status. Owner-only (admin page). */
export async function fetchLicenseStatus(): Promise<LicenseStatusResponse> {
  return apiGet<LicenseStatusResponse>("/api/license");
}

/**
 * GET /api/license/banner — minimal status for the global expiry banner.
 * Readable by any authenticated user, so the banner renders for every role
 * (AC4/AC5), unlike the Owner-only full status above.
 */
export async function fetchLicenseBanner(): Promise<LicenseBannerResponse> {
  return apiGet<LicenseBannerResponse>("/api/license/banner");
}

/**
 * GET /api/license/limits — Integration-Hub license-limit state (T10 / AT-505):
 * systems used vs systems licensed. Readable by any hub viewer (viewer+), so the
 * hub can show usage against the entitlement (AC14). The counts match what the
 * connect-time gate enforces.
 */
export async function fetchLicenseLimits(): Promise<LicenseLimitsResponse> {
  return apiGet<LicenseLimitsResponse>("/api/license/limits");
}

/**
 * GET /api/license/org-name — the dynamic organisation display name (T12 / §2).
 * Readable by any authenticated user, so every surface (header, workspace labels,
 * reports, License page) can consume this ONE resolved name instead of deriving
 * its own (§5 "One name, resolved once"). Returns a neutral default before a key
 * is installed (AC16) and updates immediately when a key with a different
 * org_name is pasted (AC15).
 */
export async function fetchLicenseOrgName(): Promise<LicenseOrgNameResponse> {
  return apiGet<LicenseOrgNameResponse>("/api/license/org-name");
}

/**
 * POST /api/license/update-key — validate-before-store. Resolves with the
 * refreshed status on success; rejects with ApiError (status 400) when the key
 * is not valid, in which case the backend stores nothing.
 */
export async function updateLicenseKey(
  key: string,
): Promise<LicenseStatusResponse> {
  const body: UpdateLicenseKeyRequest = { key };
  return apiPost<LicenseStatusResponse>("/api/license/update-key", body);
}
