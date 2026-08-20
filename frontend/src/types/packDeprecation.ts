/**
 * packDeprecation.ts — 2.0-C4 pack deprecation notices (AT-842 / AT-843).
 *
 * A pack that is being superseded carries a notice: why, the date it stops being
 * supported, and what replaces it. The notice is present ONLY for a deprecated
 * pack — the backend returns `null`/omits it otherwise, so a surface renders a
 * notice or renders nothing rather than an empty banner on every healthy pack.
 *
 * `phase` is where the deprecation has got to in time:
 *   - `grace`         — deprecated, and the pack still runs normally;
 *   - `grace_expired` — the announced grace period has passed (2.0-C4 AT-845
 *                       safe-disables it; historical findings are untouched).
 */
export type PackDeprecationPhase = 'grace' | 'grace_expired';

export interface PackDeprecationNotice {
  packId: string;
  /** The pack version this notice is about. */
  version: string;
  phase: PackDeprecationPhase;
  /** Short badge text, e.g. "Deprecated". */
  label: string;
  /** Badge plus qualifier, e.g. "Deprecated — runs until 2026-09-29". */
  statusLabel: string;
  /** Why it is being superseded. */
  reason: string;
  /** `YYYY-MM-DD` the notice started. */
  deprecatedOn: string;
  /**
   * `YYYY-MM-DD` the pack stops being supported — the last day it runs normally.
   * Empty when no removal date has been announced, in which case it never expires.
   */
  graceEndsOn: string;
  /** Whole days of grace left; `0` once expired, `null` when open-ended. */
  daysRemaining: number | null;
  /** The replacement pack, or empty when none has been named. */
  replacementPackId: string;
  /** The replacement, named for a human, e.g. "Cloud Operations (cloud_ops v1.2.0+)". */
  replacementLabel: string;
  /** One sentence carrying the reason, the dates, and the path. */
  summary: string;
}
