/**
 * R17-D4 Addendum A / T11 (AT-506) — Integration-Hub license-usage strip.
 *
 * Shows systems-used vs systems-licensed for the current org (AC14), read from
 * `GET /api/license/limits` (T10). The two numbers are the SAME ones the
 * connect-time gate (T9) enforces with, so the count shown here is the count that
 * is enforced — the "one connected entity = one system" pricing definition.
 *
 * When the org is at (or over) its licensed limit, a persistent notice carries
 * the exact backend message ("Your license covers N systems. Contact CloudFulcrum
 * to add more."), matching the connect action's disabled tooltip. An unlimited /
 * pre-addendum license (systemsLicensed null) shows the used count without a cap
 * and never a limit notice.
 *
 * Presentational only — a null `limits` (a fail-open fetch error) renders
 * nothing, since the backend remains the source of truth for enforcement
 * regardless of what the hub displays. While the endpoint is still in flight
 * (`loading`) a skeleton strip of the same shape holds the space, so the real
 * strip fills it instead of appearing later and shoving the page down.
 */
import React from 'react';
import type { LicenseLimitsResponse } from '../../types/license';
import { Skeleton } from '../common/Skeleton';
import { useLicense } from '../../context/LicenseContext';

/** The exact message the backend (license_limits.limit_message) surfaces. */
export function systemLimitMessage(systemsLicensed: number): string {
  return `Your license covers ${systemsLicensed} systems. Contact CloudFulcrum to add more.`;
}

/**
 * The exact message the backend (license_limits.unlicensed_limit_message)
 * surfaces when an UNLICENSED install hits the cap (R-1.9.1-L1 / T5, AC4). Kept
 * byte-identical to the backend so the proactive strip and the connect-time 402
 * read the same — an unlicensed install must never be told "your license covers
 * N" (it has no license); the remedy is to install one.
 */
export function unlicensedLimitMessage(cap: number): string {
  return `No license is installed. Unlicensed installations can connect up to ${cap} systems. Install a license from CloudFulcrum to connect more.`;
}

/**
 * Wording for the used-systems count.
 *
 * `unlimited` (no numeric cap) is true both for a genuine unlimited license AND
 * for NO license at all — the backend's `max_systems` is null in both cases — so
 * the count alone can't tell them apart. Using the live license STATUS
 * disambiguates, so a never-licensed install is never mislabelled as having an
 * "unlimited license":
 *   - active license (valid / grace) + no cap → "N (unlimited license)"
 *   - no active license (readonly / invalid)  → "N · no active license"
 *   - status not yet known (loading/error)    → "N" (make no license claim)
 *   - a numeric cap                           → "N of M"
 */
function usageCountLabel(
  systemsUsed: number,
  systemsLicensed: number | null,
  unlimited: boolean,
  licenseState: string | undefined,
): string {
  if (!unlimited) return `${systemsUsed} of ${systemsLicensed}`;
  if (licenseState === 'valid' || licenseState === 'grace') {
    return `${systemsUsed} (unlimited license)`;
  }
  if (licenseState === 'readonly' || licenseState === 'invalid') {
    return `${systemsUsed} · no active license`;
  }
  return `${systemsUsed}`;
}

export default function LicenseLimitBanner({
  limits,
  loading = false,
}: {
  limits: LicenseLimitsResponse | null;
  /** True while GET /api/license/limits is in flight — reserve the strip's space. */
  loading?: boolean;
}) {
  // Live license status disambiguates "unlimited license" from "no license" for
  // the count wording below. Called unconditionally (Rules of Hooks); useLicense
  // returns a safe default ({status: null}) outside a provider.
  const licenseState = useLicense().status?.status;

  if (loading && !limits) {
    return (
      <div
        aria-busy="true"
        aria-label="Loading license usage"
        className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-xl border border-border bg-panel px-4 py-2.5 shadow-sm"
      >
        <Skeleton className="h-3 w-40" />
        <Skeleton className="h-3 w-64" />
      </div>
    );
  }

  if (!limits) return null;

  const { systemsUsed, systemsLicensed, unlimited, canConnectMore } = limits;
  const atLimit = !unlimited && !canConnectMore;
  // An org with no ACTIVE license (readonly / invalid) that is capped is at the
  // unlicensed cap (R-1.9.1-L1 / T5), not a licensed limit — so the notice must
  // name the missing license rather than claim "your license covers N". A valid /
  // grace license (or the brief status-unknown window) keeps the licensed wording.
  const noActiveLicense = licenseState === 'readonly' || licenseState === 'invalid';

  return (
    <div
      data-testid="license-usage-strip"
      role="status"
      className={[
        'flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-xl border px-4 py-2.5 text-xs shadow-sm',
        atLimit
          ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
          : 'border-border bg-panel text-muted',
      ].join(' ')}
    >
      <div className="flex items-center gap-2">
        <span className="uppercase tracking-wide">Systems used</span>
        <span data-testid="license-usage-count" className="font-semibold text-text">
          {usageCountLabel(systemsUsed, systemsLicensed, unlimited, licenseState)}
        </span>
      </div>
      {atLimit && systemsLicensed != null && (
        <span data-testid="license-at-limit" className="font-medium">
          {noActiveLicense
            ? unlicensedLimitMessage(systemsLicensed)
            : systemLimitMessage(systemsLicensed)}
        </span>
      )}
    </div>
  );
}
