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
  LicenseStatusResponse,
  UpdateLicenseKeyRequest,
} from "../types/license";

export type { LicenseStatusResponse, UpdateLicenseKeyRequest };

/** GET /api/license — current license status for the Owner. */
export async function fetchLicenseStatus(): Promise<LicenseStatusResponse> {
  return apiGet<LicenseStatusResponse>("/api/license");
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
