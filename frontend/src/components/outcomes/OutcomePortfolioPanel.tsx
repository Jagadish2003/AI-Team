import React from 'react';
import { Activity, Layers, ShieldAlert } from 'lucide-react';
import { fetchOutcomePortfolio } from '../../api/outcomeApi';
import { cacheKeys } from '../../lib/cacheKeys';
import { useResource } from '../../lib/dataCache';
import type { OutcomePortfolioView } from '../../types/outcome';
import { OutcomeNumberDisclosure } from './OutcomeNumberDisclosure';
import OutcomeCaveatDetails from './OutcomeCaveatDetails';

function runPair(measurement?: {
  baselineRunId?: string | null;
  currentRunId?: string | null;
} | null) {
  if (!measurement) return 'No movement record';
  return `${measurement.baselineRunId ?? 'n/a'} -> ${measurement.currentRunId ?? 'n/a'}`;
}

export default function OutcomePortfolioPanel() {
  const { data, loading, error } = useResource<OutcomePortfolioView>(
    cacheKeys.outcomePortfolio,
    () => fetchOutcomePortfolio({ limit: 200 }),
  );

  if (error) {
    return (
      <section className="rounded-xl border border-border bg-panel px-5 py-4">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
          <Layers className="h-4 w-4 text-accent" aria-hidden />
          Outcome Portfolio
        </div>
        <div className="mt-2 text-sm text-danger">
          Outcome portfolio is unavailable for this role or session.
        </div>
      </section>
    );
  }

  const aggregates = data?.aggregates;
  const aggregateRefs = aggregates?.numberRefs ?? [];

  return (
    <section className="rounded-xl border border-border bg-panel">
      <div className="flex flex-wrap items-start justify-between gap-4 px-5 py-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
            <Layers className="h-4 w-4 text-accent" aria-hidden />
            Outcome Portfolio
          </div>
          <div className="mt-1 text-sm text-muted">
            Stored movement comparisons for actioned opportunities.
          </div>
        </div>
        {aggregates?.caveatedMeasurementCount ? (
          <div className="flex shrink-0 items-center gap-2 rounded-md border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs font-semibold text-amber-700 dark:text-amber-300">
            <ShieldAlert className="h-4 w-4" aria-hidden />
            {aggregates.caveatedMeasurementCount} caveated measurement
            {aggregates.caveatedMeasurementCount === 1 ? '' : 's'}
          </div>
        ) : null}
      </div>

      <div className="border-t border-border px-5 py-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          {loading && !data
            ? ['Actioned opportunities', 'Measured opportunities', 'Measurements', 'Caveated'].map((label) => (
                <div key={label} className="rounded-md border border-border bg-bg/40 px-3 py-2">
                  <div className="text-xs text-muted">{label}</div>
                  <div className="mt-1 h-5 w-16 animate-pulse rounded bg-border" />
                </div>
              ))
            : aggregateRefs.slice(0, 4).map((refItem) => (
                <OutcomeNumberDisclosure key={refItem.id} refItem={refItem} />
              ))}
        </div>

        <div className="mt-4 overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-muted">
              <tr>
                <th className="py-2 pr-4 font-semibold">Opportunity</th>
                <th className="py-2 pr-4 font-semibold">State</th>
                <th className="py-2 pr-4 font-semibold">Movement</th>
                <th className="py-2 pr-4 font-semibold">Caveats</th>
                <th className="py-2 pr-4 font-semibold">Run Pair</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(data?.items ?? []).map((item) => {
                const movementRef =
                  item.latestMeasurement?.numberRefs.find((refItem) => refItem.field === 'delta') ??
                  item.latestMeasurement?.numberRefs[0];
                return (
                  <tr key={item.opportunityIdentity}>
                    <td className="max-w-[280px] py-2 pr-4 font-mono text-xs text-text">
                      {item.opportunityIdentity}
                    </td>
                    <td className="py-2 pr-4 text-muted">{item.state ?? 'unknown'}</td>
                    <td className="min-w-[220px] py-2 pr-4 text-text">
                      {movementRef ? (
                        <OutcomeNumberDisclosure refItem={movementRef} />
                      ) : (
                        'Not measured'
                      )}
                    </td>
                    <td className="py-2 pr-4 align-top">
                      <OutcomeCaveatDetails
                        confounders={item.latestMeasurement?.confounders}
                      />
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs text-muted">
                      {runPair(item.latestMeasurement)}
                    </td>
                  </tr>
                );
              })}
              {!loading && data?.items.length === 0 ? (
                <tr>
                  <td className="py-4 text-sm text-muted" colSpan={5}>
                    No actioned opportunities are available in this portfolio.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
      <div className="border-t border-border px-5 py-3 text-xs text-muted">
        <Activity className="mr-1 inline h-3.5 w-3.5 text-accent" aria-hidden />
        Aggregate caveat counts are computed from the stored measurements in the table.
      </div>
    </section>
  );
}
