import React from 'react';
import { Activity, ShieldAlert } from 'lucide-react';
import type { OutcomeReportSection } from '../../types/outcome';
import { OutcomeNumberDisclosure } from './OutcomeNumberDisclosure';

export default function ExecutiveOutcomeSection({
  section,
}: {
  section?: OutcomeReportSection | null;
}) {
  if (!section) return null;
  const aggregateRefs = section.aggregates?.numberRefs ?? [];

  return (
    <section className="rounded-xl border border-border bg-panel">
      <div className="flex flex-wrap items-start justify-between gap-4 px-5 py-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
            <Activity className="h-4 w-4 text-accent" aria-hidden />
            Outcome Movement
          </div>
          <div className="mt-1 text-sm text-muted">{section.summary}</div>
        </div>
        <div className="flex shrink-0 items-center gap-2 rounded-md border border-border bg-bg/40 px-3 py-2 text-xs font-semibold text-muted">
          <ShieldAlert className="h-4 w-4 text-accent" aria-hidden />
          {section.aggregates.caveatedMeasurementCount ? 'Caveats present' : 'No caveats'}
        </div>
      </div>

      <div className="border-t border-border px-5 py-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          {aggregateRefs.slice(0, 4).map((refItem) => (
            <OutcomeNumberDisclosure key={refItem.id} refItem={refItem} />
          ))}
        </div>

        {section.highlights.length > 0 ? (
          <div className="mt-4 overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th className="py-2 pr-4 font-semibold">Signal</th>
                  <th className="py-2 pr-4 font-semibold">Movement</th>
                  <th className="py-2 pr-4 font-semibold">Projection</th>
                  <th className="py-2 pr-4 font-semibold">Run Pair</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {section.highlights.map((measurement) => {
                  const movementRef =
                    measurement.numberRefs.find((refItem) => refItem.field === 'delta') ??
                    measurement.numberRefs[0];
                  return (
                    <tr key={`${measurement.opportunityIdentity}-${measurement.currentRunId}`}>
                      <td className="py-2 pr-4 text-text">
                        {measurement.primaryMovement?.signalName ?? 'primary signal'}
                      </td>
                      <td className="min-w-[220px] py-2 pr-4 text-text">
                        {movementRef ? (
                          <OutcomeNumberDisclosure refItem={movementRef} />
                        ) : (
                          'Unavailable'
                        )}
                      </td>
                      <td className="py-2 pr-4 text-muted">
                        {measurement.projectionValidation?.verdict ?? 'unknown'}
                      </td>
                      <td className="py-2 pr-4 font-mono text-xs text-muted">
                        {measurement.baselineRunId ?? 'n/a'} {'->'} {measurement.currentRunId ?? 'n/a'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="mt-4 text-sm text-muted">
            No stored movement measurements are available for this report run yet.
          </div>
        )}
      </div>
    </section>
  );
}
