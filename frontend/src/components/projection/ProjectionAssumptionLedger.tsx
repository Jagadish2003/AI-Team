import React from 'react';
import type {
  InterventionProjection,
  ProjectionAssumption,
} from '../../types/enrichment';

export function projectionAssumptions(
  projection: InterventionProjection | null | undefined,
): ProjectionAssumption[] {
  const assumptions = projection?.assumptionLedger;
  if (!Array.isArray(assumptions)) return [];
  return assumptions.filter(
    (assumption): assumption is ProjectionAssumption =>
      Boolean(
        assumption &&
          typeof assumption.id === 'string' &&
          typeof assumption.label === 'string' &&
          typeof assumption.description === 'string' &&
          assumption.label.trim() &&
          assumption.description.trim(),
      ),
  );
}

export function ProjectionAssumptionList({
  projection,
}: {
  projection: InterventionProjection | null | undefined;
}) {
  const assumptions = projectionAssumptions(projection);
  if (assumptions.length === 0) return null;

  return (
    <ul className="space-y-2" data-testid="projection-assumption-list">
      {assumptions.map((assumption) => (
        <li
          key={assumption.id}
          className="rounded-md border border-border/70 bg-panel/70 px-3 py-2"
        >
          <div className="text-xs font-semibold leading-snug text-text">
            {assumption.label}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            {assumption.description}
          </p>
        </li>
      ))}
    </ul>
  );
}

export default function ProjectionAssumptionLedger({
  projection,
}: {
  projection: InterventionProjection | null | undefined;
}) {
  const assumptions = projectionAssumptions(projection);
  if (assumptions.length === 0) return null;

  return (
    <div data-testid="projection-assumption-ledger">
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Assumptions</span>
        <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
          {assumptions.length} listed
        </span>
      </div>
      <div className="rounded-lg border border-border bg-bg/30 p-3">
        <ProjectionAssumptionList projection={projection} />
      </div>
    </div>
  );
}
