/**
 * LIC-1 / T9 (AT-350) — global license expiry banner.
 *
 * Rendered once at the authenticated layout level (inside AuthGuard), so it
 * appears on every authenticated page without per-page wiring. Reads the shared
 * LicenseContext (single fetch, not refetched per route).
 *
 *   grace     → amber: "Your AgentIQ license expired on {date}. Contact
 *                       CloudFulcrum to renew." (app stays fully functional)
 *   readonly  → red:   "License expired. Renew to resume discovery runs."
 *   invalid   → red:   same read-only message (no usable license).
 *   valid     → nothing (banner disappears once a valid key is installed).
 *
 * Colours use the same amber/red tones as the T8 status badge and the theme's
 * semantic tokens, so the banner respects dark/light mode (ThemeContext).
 */
import { useLicense } from "../../context/LicenseContext";

export default function LicenseBanner() {
  const { status } = useLicense();
  const state = status?.status;

  if (state === "grace") {
    return (
      <div
        role="alert"
        data-testid="license-banner"
        data-state="grace"
        className="border-b border-amber-500/30 bg-amber-500/15 px-4 py-2 text-center text-sm font-medium text-amber-200"
      >
        {`Your AgentIQ license expired on ${status?.expires_at ?? ""}. Contact CloudFulcrum to renew.`}
      </div>
    );
  }

  if (state === "readonly" || state === "invalid") {
    return (
      <div
        role="alert"
        data-testid="license-banner"
        data-state="readonly"
        className="border-b border-red-500/30 bg-red-500/15 px-4 py-2 text-center text-sm font-medium text-red-300"
      >
        License expired. Renew to resume discovery runs.
      </div>
    );
  }

  // valid / loading / status unavailable → no banner.
  return null;
}
