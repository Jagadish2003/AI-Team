import React from 'react';
import { Activity, ShieldAlert } from 'lucide-react';
import { fetchOpportunityOutcome } from '../../api/outcomeApi';
import { cacheKeys } from '../../lib/cacheKeys';
import { useResource } from '../../lib/dataCache';
import type { OpportunityOutcomeView } from '../../types/outcome';
import { OutcomeNumberDisclosure } from './OutcomeNumberDisclosure';
import OutcomeCaveatDetails from './OutcomeCaveatDetails';

function statusText(view: OpportunityOutcomeView): string {
  if (view.latestMeasurement) {
    const movement = view.latestMeasurement.primaryMovement;
    const signal = movement?.signalName ?? 'primary signal';
    return `${signal} has a stored movement comparison against its baseline following the recorded action.`;
  }
  return view.emptyState?.message ?? 'No stored movement measurement exists yet.';
}

export default function OpportunityOutcomePanel({
  opportunityIdentity,
}: {
  opportunityIdentity?: string | null;
}) {
  const enabled = Boolean(opportunityIdentity);
  const { data, loading, error } = useResource<OpportunityOutcomeView>(
    enabled ? cacheKeys.opportunityOutcome(opportunityIdentity as string) : null,
    () => fetchOpportunityOutcome(opportunityIdentity as string),
  );

  if (!enabled) return null;
  const status = (error as unknown as { status?: number } | null)?.status;
  if (status === 404) return null;

  return (
    <section className="mt-4 rounded-xl border border-border bg-panel">
      <div className="flex items-start justify-between gap-4 px-5 py-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
            <Activity className="h-4 w-4 text-accent" aria-hidden />
            Opportunity Outcome
          </div>
          <div className="mt-1 text-sm text-muted">
            {loading ? 'Loading stored movement...' : data ? statusText(data) : 'Outcome unavailable.'}
          </div>
        </div>
        {data?.caveatedMeasurementCount ? (
          <div className="flex shrink-0 items-center gap-2 rounded-md border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs font-semibold text-amber-700 dark:text-amber-300">
            <ShieldAlert className="h-4 w-4" aria-hidden />
            {data.caveatedMeasurementCount} caveated measurement
            {data.caveatedMeasurementCount === 1 ? '' : 's'}
          </div>
        ) : null}
      </div>

      {error && status !== 404 ? (
        <div className="border-t border-border px-5 py-4 text-sm text-danger">
          Outcome view is unavailable for this role or session.
        </div>
      ) : null}

      {data?.latestMeasurement ? (
        <div className="border-t border-border px-5 py-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
            {data.latestMeasurement.numberRefs.slice(0, 4).map((refItem) => (
              <OutcomeNumberDisclosure key={refItem.id} refItem={refItem} />
            ))}
          </div>
          <div className="mt-4 overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th className="py-2 pr-4 font-semibold">Measured At</th>
                  <th className="py-2 pr-4 font-semibold">Comparability</th>
                  <th className="py-2 pr-4 font-semibold">Projection</th>
                  <th className="py-2 pr-4 font-semibold">Caveats</th>
                  <th className="py-2 pr-4 font-semibold">Run Pair</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.measurements.slice().reverse().map((measurement) => (
                  <tr key={`${measurement.currentRunId}-${measurement.measuredAt}`}>
                    <td className="py-2 pr-4 text-text">
                      {measurement.measuredAt
                        ? new Date(measurement.measuredAt).toLocaleDateString()
                        : 'Unavailable'}
                    </td>
                    <td className="py-2 pr-4 text-muted">
                      {measurement.comparability?.verdict ?? 'unknown'}
                    </td>
                    <td className="py-2 pr-4 text-muted">
                      {measurement.projectionValidation?.verdict ?? 'unknown'}
                    </td>
                    <td className="py-2 pr-4 align-top">
                      <OutcomeCaveatDetails confounders={measurement.confounders} />
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs text-muted">
                      {measurement.baselineRunId ?? 'n/a'} {'->'} {measurement.currentRunId ?? 'n/a'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : data?.emptyState ? (
        <div className="border-t border-border px-5 py-4 text-sm text-muted">
          {data.emptyState.message}
        </div>
      ) : null}
    </section>
  );
}
