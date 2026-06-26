/**
 * LIC-1 / T9 (AT-350) — global license expiry banner.
 *
 * Rendered once at the authenticated layout level (inside AuthGuard), so it
 * appears on every authenticated page without per-page wiring. Reads the shared
 * LicenseContext (single fetch, not refetched per route).
 *
 * Copy is driven by status + reason so a never-licensed install is not mislabelled
 * as "expired" (§5 / AC6):
 *
 *   grace                         → amber: "Your AgentIQ license expired on
 *                                   {date}. Contact CloudFulcrum to renew."
 *   readonly / invalid, no_license or
 *     signature_or_format (no usable key) → red: "No valid license installed.
 *                                   Paste a valid license key to activate AgentIQ."
 *   readonly, clock_rollback      → red: clock-inconsistency message.
 *   readonly, expired past grace  → red: "License expired. Renew to resume
 *                                   discovery runs."
 *   valid                         → nothing (banner disappears once valid).
 *
 * Colours use the same amber/red tones as the T8 status badge and the theme's
 * semantic tokens, so the banner respects dark/light mode (ThemeContext).
 */
import { useLicense } from "../../context/LicenseContext";

const AMBER = "border-b border-amber-500/30 bg-amber-500/15 px-4 py-2 text-center text-sm font-medium text-amber-200";
const RED = "border-b border-red-500/30 bg-red-500/15 px-4 py-2 text-center text-sm font-medium text-red-300";

const UNLICENSED_MSG = "No valid license installed. Paste a valid license key to activate AgentIQ.";
const CLOCK_MSG = "License validation is paused — the system clock looks inconsistent. Restore the correct date to resume.";
const EXPIRED_MSG = "License expired. Renew to resume discovery runs.";

/**
 * Format the license expiry (a plain ISO calendar date, e.g. "2026-06-10") as a
 * human-readable locale date, matching the date formatting used elsewhere in the
 * UI. Uses an explicit "en-US" locale + UTC time zone so the rendered day is
 * deterministic and never shifts across server/CI time zones. Falls back to the
 * raw value if it cannot be parsed.
 */
function formatExpiry(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}

export default function LicenseBanner() {
  const { status } = useLicense();
  const state = status?.status;
  const reason = status?.reason ?? null;

  if (state === "grace") {
    // Grace = past expiry but discovery runs STILL WORK. Say so explicitly, with
    // a countdown when available, so admins neither panic ("expired!") nor grow
    // complacent ("runs still work, nothing to do"). N days come from the backend
    // (expires_at + grace_days − today); fall back to a non-numeric nudge if absent.
    const n = status?.grace_days_remaining;
    const tail =
      typeof n === "number" && n > 0
        ? `Discovery runs will be blocked in ${n} day${n === 1 ? "" : "s"} — contact CloudFulcrum to renew.`
        : "Discovery runs still work during the grace period — contact CloudFulcrum to renew.";
    return (
      <div role="alert" data-testid="license-banner" data-state="grace" className={AMBER}>
        {`Your AgentIQ license expired on ${formatExpiry(status?.expires_at)}. ${tail}`}
      </div>
    );
  }

  if (state === "readonly" || state === "invalid") {
    // A never-licensed install (no key, or an unverifiable/tampered key) must not
    // claim the term "expired" — there was no term. invalid always means an
    // unusable key; readonly covers no_license / clock_rollback / past-grace.
    let message = EXPIRED_MSG;
    let dataReason = reason ?? "expired";
    if (state === "invalid" || reason === "no_license" || reason === "signature_or_format") {
      message = UNLICENSED_MSG;
      dataReason = reason ?? "invalid";
    } else if (reason === "clock_rollback") {
      message = CLOCK_MSG;
    }
    return (
      <div role="alert" data-testid="license-banner" data-state="readonly" data-reason={dataReason} className={RED}>
        {message}
      </div>
    );
  }

  // valid / loading / status unavailable → no banner.
  return null;
}
