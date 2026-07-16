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

/** The exact message the backend (license_limits.limit_message) surfaces. */
export function systemLimitMessage(systemsLicensed: number): string {
  return `Your license covers ${systemsLicensed} systems. Contact CloudFulcrum to add more.`;
}

export default function LicenseLimitBanner({
  limits,
  loading = false,
}: {
  limits: LicenseLimitsResponse | null;
  /** True while GET /api/license/limits is in flight — reserve the strip's space. */
  loading?: boolean;
}) {
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
          {unlimited ? `${systemsUsed} (unlimited license)` : `${systemsUsed} of ${systemsLicensed}`}
        </span>
      </div>
      {atLimit && systemsLicensed != null && (
        <span data-testid="license-at-limit" className="font-medium">
          {systemLimitMessage(systemsLicensed)}
        </span>
      )}
    </div>
  );
}
