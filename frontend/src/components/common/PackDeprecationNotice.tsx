import React from 'react';
import type { PackDeprecationPhase } from '../../types/packDeprecation';

/**
 * PackDeprecationBadge / PackDeprecationDetail — 2.0-C4 T2 (AT-843 / AC1).
 *
 * ONE pair of components for every surface that must show a pack is being
 * superseded: run configuration (Discovery Plan), run health (packs panel), and the
 * pack's findings (opportunity detail). A single implementation is the point — the
 * same reasoning as `PackCertificationBadge`: a customer who reads "runs until
 * 2026-09-29" on the pack picker must meet the identical wording on the finding
 * that pack produced, not a differently-phrased near-miss.
 *
 * Two components rather than one because the surfaces genuinely differ in space:
 * a findings row and a pack pill need the compact BADGE, while run configuration
 * and run health have room for the full sentence with the replacement in it. Both
 * take the backend's own strings, so neither can word the notice differently.
 *
 * Deprecation is amber, never red. The pack still works — during grace it runs
 * exactly as before — and colouring a working pack as an error would make the
 * notice read as a fault rather than as advance warning. An EXPIRED grace is the
 * stronger amber-bordered state: still not an error, because the pack being
 * safe-disabled is the announced, expected end of the process (AT-845).
 */
const TONE: Record<PackDeprecationPhase, string> = {
  grace: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  grace_expired:
    'border-amber-600/60 bg-amber-600/15 text-amber-800 dark:text-amber-200',
};

const FALLBACK_LABEL: Record<PackDeprecationPhase, string> = {
  grace: 'Deprecated',
  grace_expired: 'Deprecated — grace period ended',
};

export function PackDeprecationBadge({
  phase,
  label,
  notice,
  testId,
}: {
  phase?: PackDeprecationPhase | string | null;
  /** Backend-supplied `statusLabel`. Falls back to the canonical phase wording. */
  label?: string | null;
  /** The full sentence, surfaced as the tooltip so the pill stays compact. */
  notice?: string | null;
  testId?: string;
}) {
  // An absent or unrecognised phase renders NOTHING. A pack with no notice is not
  // "deprecated by default" here — the backend decides that, and a badge invented
  // in the UI would tell a customer their pack is going away when it is not.
  if (!phase || !(phase in TONE)) return null;
  const known = phase as PackDeprecationPhase;

  return (
    <span
      data-testid={testId ?? `pack-deprecation-${known}`}
      data-phase={known}
      title={notice ?? undefined}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-none ${TONE[known]}`}
    >
      {label || FALLBACK_LABEL[known]}
    </span>
  );
}

/**
 * The full notice, for surfaces with room for it.
 *
 * Always states the two things AC1 names — the date support ends and what replaces
 * it — and states them EXPLICITLY when they are absent ("no removal date has been
 * announced", "no replacement pack has been named") rather than leaving a gap the
 * reader has to interpret.
 */
export function PackDeprecationDetail({
  phase,
  notice,
  graceEndsOn,
  replacementLabel,
  daysRemaining,
  testId,
}: {
  phase?: PackDeprecationPhase | string | null;
  /** The backend's one-sentence summary. */
  notice?: string | null;
  graceEndsOn?: string | null;
  replacementLabel?: string | null;
  daysRemaining?: number | null;
  testId?: string;
}) {
  if (!phase || !(phase in TONE)) return null;
  const expired = phase === 'grace_expired';

  return (
    <div
      data-testid={testId ?? 'pack-deprecation-detail'}
      data-phase={phase}
      className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-700 dark:text-amber-300"
    >
      <p>{notice}</p>
      <p className="mt-1 opacity-90">
        <span data-testid="pack-deprecation-ends-on">
          {expired
            ? `Support ended ${graceEndsOn ?? 'on the announced date'}.`
            : graceEndsOn
              ? `Supported until ${graceEndsOn}${
                  typeof daysRemaining === 'number'
                    ? ` (${daysRemaining} day${daysRemaining === 1 ? '' : 's'} left)`
                    : ''
                }.`
              : 'No removal date has been announced.'}
        </span>{' '}
        <span data-testid="pack-deprecation-replacement">
          {replacementLabel
            ? `Replaced by ${replacementLabel}.`
            : 'No replacement pack has been named.'}
        </span>
      </p>
    </div>
  );
}

export default PackDeprecationBadge;
