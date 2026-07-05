/**
 * LIC-1 / T9 (AT-350) — shared license status provider.
 *
 * Single source of license status for the authenticated app shell. Mounted once
 * inside AuthGuard (so it only fetches for an authenticated session and is shared
 * across all protected routes — never refetched per page). The global
 * LicenseBanner consumes this; the LicensePage (T8) keeps its own Owner-only
 * fetch for the editable admin view.
 *
 * Status comes from the T9 `GET /api/license/banner` endpoint via the
 * `licenseApi` wrapper. That endpoint is auth-only (NOT Owner-gated), so the
 * banner renders for every authenticated role — including the analysts who can
 * start runs and need to see why a run is blocked (AC4/AC5). The authoritative
 * read-only enforcement is the server-side gate (T5); this banner is the
 * user-facing nudge. Only a transient/network error leaves status null (no
 * banner). The full admin detail stays Owner-only on `GET /api/license` (T6/T8).
 *
 * R17-D4 Addendum A §2 / T13 (AT-508) — this same shared provider now also owns
 * the dynamic organisation display name (T12's `GET /api/license/org-name`). It
 * is the "one name, resolved once" (§5) that every UI surface consumes via
 * `useOrgName()`: fetched once alongside the banner and re-read by `refresh()`
 * after a key update, so pasting a key with a different `org_name` updates the
 * header, workspace labels, reports, and License page immediately with no restart
 * (AC15). The banner and org-name reads are independent — one failing never
 * blanks the other.
 */
import React, { createContext, useContext, useEffect, useState } from "react";

import { fetchLicenseBanner, fetchLicenseOrgName } from "../api/licenseApi";
import type { LicenseBannerResponse } from "../types/license";
import { resolveOrgName } from "../utils/orgName";

interface LicenseContextValue {
  status: LicenseBannerResponse | null;
  /**
   * Resolved organisation display name from T12's `GET /api/license/org-name`
   * (already a usable string server-side, incl. the neutral default). Null only
   * before the first fetch resolves or on a transient error — consume via
   * `useOrgName()`, which applies the neutral default in that window.
   */
  orgName: string | null;
  loading: boolean;
  /** Re-read status + org name after a key update so both refresh immediately. */
  refresh: () => Promise<void>;
}

const LicenseContext = createContext<LicenseContextValue>({
  status: null,
  orgName: null,
  loading: false,
  refresh: async () => undefined,
});

export function LicenseProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<LicenseBannerResponse | null>(null);
  const [orgName, setOrgName] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh(): Promise<void> {
    setLoading(true);
    // Banner + org name are read together but independently: a failure of one
    // must not blank the other (allSettled never rejects).
    const [bannerResult, orgNameResult] = await Promise.allSettled([
      fetchLicenseBanner(),
      fetchLicenseOrgName(),
    ]);
    // No usable banner (network/transient error) → render no banner.
    setStatus(bannerResult.status === "fulfilled" ? bannerResult.value : null);
    // No usable name → null; useOrgName() falls back to the neutral default.
    setOrgName(orgNameResult.status === "fulfilled" ? orgNameResult.value.orgName : null);
    setLoading(false);
  }

  useEffect(() => {
    void refresh();
    // Fetch once when the authenticated shell mounts; shared across all routes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <LicenseContext.Provider value={{ status, orgName, loading, refresh }}>
      {children}
    </LicenseContext.Provider>
  );
}

export function useLicense(): LicenseContextValue {
  return useContext(LicenseContext);
}

/**
 * R17-D4 Addendum A §2 / T13 (AT-508) — the resolved organisation display name
 * for the app shell. Reads the shared, license-resolved name (one fetch, all
 * roles) and applies the neutral default for the loading/error window via
 * `resolveOrgName`, so every surface (header, workspace labels, reports, License
 * page) shows the same name and updates the instant a new key is pasted (AC15);
 * before any key is installed it shows the neutral default (AC16).
 */
export function useOrgName(): string {
  const { orgName } = useLicense();
  return resolveOrgName(orgName);
}
