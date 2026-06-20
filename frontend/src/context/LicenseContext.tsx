/**
 * LIC-1 / T9 (AT-350) — shared license status provider.
 *
 * Single source of license status for the authenticated app shell. Mounted once
 * inside AuthGuard (so it only fetches for an authenticated session and is shared
 * across all protected routes — never refetched per page). The global
 * LicenseBanner consumes this; the LicensePage (T8) keeps its own Owner-only
 * fetch for the editable admin view.
 *
 * Status comes from the T6 `GET /api/license` endpoint via T8's `licenseApi`
 * wrapper. That endpoint is Owner-gated, so for non-Owner sessions (or any
 * transient error) the fetch fails and status stays null → the banner simply
 * does not render. The authoritative read-only enforcement is the server-side
 * gate (T5); this banner is the user-facing nudge.
 */
import React, { createContext, useContext, useEffect, useState } from "react";

import { fetchLicenseStatus } from "../api/licenseApi";
import type { LicenseStatusResponse } from "../types/license";

interface LicenseContextValue {
  status: LicenseStatusResponse | null;
  loading: boolean;
  /** Re-read status after a key update so the banner clears immediately. */
  refresh: () => Promise<void>;
}

const LicenseContext = createContext<LicenseContextValue>({
  status: null,
  loading: false,
  refresh: async () => undefined,
});

export function LicenseProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<LicenseStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh(): Promise<void> {
    setLoading(true);
    try {
      setStatus(await fetchLicenseStatus());
    } catch {
      // No usable status (non-Owner 403, network, etc.) → render no banner.
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // Fetch once when the authenticated shell mounts; shared across all routes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <LicenseContext.Provider value={{ status, loading, refresh }}>
      {children}
    </LicenseContext.Provider>
  );
}

export function useLicense(): LicenseContextValue {
  return useContext(LicenseContext);
}
