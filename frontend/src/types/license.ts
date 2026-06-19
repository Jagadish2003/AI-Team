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
