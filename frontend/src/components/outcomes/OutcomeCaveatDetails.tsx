import React from 'react';
import { ShieldAlert } from 'lucide-react';
import type { OutcomeConfounder } from '../../types/outcome';
import {
  outcomeCaveatExplanation,
  outcomeCaveatLabel,
  outcomeCaveatSeverity,
} from '../../utils/outcomeCaveats';

export default function OutcomeCaveatDetails({
  confounders,
}: {
  confounders?: OutcomeConfounder[] | null;
}) {
  const caveats = (confounders ?? []).filter(Boolean);
  if (caveats.length === 0) return <span className="text-muted">None</span>;

  return (
    <details
      className="min-w-[240px] rounded-md border border-amber-400/30 bg-amber-400/10"
      data-testid="outcome-caveat-details"
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-2 text-xs font-semibold text-amber-700 marker:hidden dark:text-amber-300">
        <ShieldAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
        {caveats.length} labelled caveat{caveats.length === 1 ? '' : 's'}
      </summary>
      <ul className="space-y-2 border-t border-amber-400/20 px-3 py-2.5">
        {caveats.map((caveat, index) => {
          const explanation = outcomeCaveatExplanation(caveat);
          return (
            <li key={`${caveat.type ?? caveat.label ?? 'caveat'}-${index}`}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <span className="text-xs font-semibold leading-relaxed text-text">
                  {outcomeCaveatLabel(caveat)}
                </span>
                <span className="rounded border border-amber-400/25 bg-panel/60 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
                  {outcomeCaveatSeverity(caveat)}
                </span>
              </div>
              {explanation ? (
                <p className="mt-1 text-xs leading-relaxed text-muted">
                  Why this matters: {explanation}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
