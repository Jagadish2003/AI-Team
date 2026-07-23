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
 * Presentational only — a null `limits` (endpoint not yet loaded, or a fail-open
 * fetch error) renders nothing, since the backend remains the source of truth for
 * enforcement regardless of what the hub displays.
 */
import React from 'react';
import type { LicenseLimitsResponse } from '../../types/license';

/** The exact message the backend (license_limits.limit_message) surfaces. */
export function systemLimitMessage(systemsLicensed: number): string {
  return `Your license covers ${systemsLicensed} systems. Contact CloudFulcrum to add more.`;
}

export default function LicenseLimitBanner({
  limits,
}: {
  limits: LicenseLimitsResponse | null;
}) {
  if (!limits) return null;

  const { systemsUsed, systemsLicensed, unlimited, canConnectMore } = limits;
  const atLimit = !unlimited && !canConnectMore;
  // MSP-B13 / T4 (AT-746): approaching-capacity notice — shown when the org is
  // under the cap but within the configured margin of it (never together with the
  // at-limit hard stop). The wording comes from the backend `notice` field.
  const approaching = !atLimit && Boolean(limits.approachingCap) && Boolean(limits.notice);

  return (
    <div
      data-testid="license-usage-strip"
      role="status"
      className={[
        'flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-xl border px-4 py-2.5 text-xs shadow-sm',
        atLimit
          ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
          : approaching
            ? 'border-amber-500/20 bg-amber-500/5 text-amber-200'
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
      {approaching && (
        <span data-testid="license-approaching-limit" className="font-medium">
          {limits.notice}
        </span>
      )}
    </div>
  );
}
