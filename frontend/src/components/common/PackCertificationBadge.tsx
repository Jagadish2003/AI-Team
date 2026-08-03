import React from 'react';
import type { PackCertificationLevel } from '../../types/packCertification';

/**
 * PackCertificationBadge — 2.0-C2 T3 (AT-833 / AC2).
 *
 * ONE component for every surface that shows a pack's certification level:
 * selection (Discovery Plan), activation and attribution (run health), findings
 * (opportunity detail), and exports (executive report). A single component is the
 * point — a board paper and the run-configuration screen must not word the same
 * badge differently, and a reader who learns the pill in one place should not have
 * to re-learn it in another.
 *
 * The `level` passed in is always the backend's EFFECTIVE level. An unverifiable
 * Certified claim arrives here as `community` and renders as Community; this
 * component has no path to render a claim it was not given (2.0-C2 AC1).
 *
 * `reviewDue` is an ADDITIVE qualifier, never a downgrade: the badge keeps its
 * level and gains "review due" (2.0-C2 AC4's display half).
 */
const TONE: Record<PackCertificationLevel, string> = {
  // Deliberately not "success green": Certified is a provenance statement, not a
  // health status, and it sits beside health pills that do mean green/amber.
  certified: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  partner: 'border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-300',
  community: 'border-border bg-panel2 text-muted',
};

const FALLBACK_LABEL: Record<PackCertificationLevel, string> = {
  certified: 'CloudFulcrum Certified',
  partner: 'Partner',
  community: 'Community',
};

export default function PackCertificationBadge({
  level,
  label,
  reviewDue = false,
  reviewDueDetail,
  testId,
}: {
  level?: PackCertificationLevel | string | null;
  /** Backend-supplied label. Falls back to the canonical wording for the level. */
  label?: string | null;
  reviewDue?: boolean;
  /**
   * 2.0-C2 T5: why the review is due. Surfaced as the tooltip so the pill stays
   * compact while still telling an operator what to do — "re-review against a newer
   * platform" and "re-issue an aged certification" are different jobs.
   */
  reviewDueDetail?: string | null;
  testId?: string;
}) {
  // An absent or unrecognised level renders NOTHING rather than guessing. A pack
  // with no resolvable badge is not "Community by default" here — the backend
  // decides that, and inventing it in the UI would be a claim we cannot support.
  if (!level || !(level in TONE)) return null;
  const known = level as PackCertificationLevel;

  return (
    <span
      data-testid={testId ?? `pack-certification-${known}`}
      data-level={known}
      title={
        reviewDue
          ? reviewDueDetail ?? 'This certification is due for review'
          : undefined
      }
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-none ${TONE[known]}`}
    >
      {label || FALLBACK_LABEL[known]}
      {reviewDue ? (
        <span data-testid="pack-certification-review-due" className="opacity-80">
          · review due
        </span>
      ) : null}
    </span>
  );
}
