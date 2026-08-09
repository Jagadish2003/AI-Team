import React from 'react';
import type { InterventionProjection } from '../../types/enrichment';

export interface ProjectionBasisItem {
  id: string;
  label: string;
  value: string;
  detail?: string;
}

function isPresentNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function labelize(value: string | null | undefined): string {
  const text = (value ?? '').trim();
  if (!text) return 'Not available';
  return text
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat('en-US', {
    maximumFractionDigits: 1,
  }).format(value);
}

function formatMetricValue(
  value: number | null | undefined,
  unit: string | null | undefined,
): string {
  if (!isPresentNumber(value)) return 'Not available';
  const formatted = formatNumber(value);
  switch (unit) {
    case 'days':
      return `${formatted} days`;
    case 'hours':
      return `${formatted} hours`;
    case 'pct':
      return `${formatted}%`;
    case 'ratio':
      return formatted;
    default:
      return formatted;
  }
}

function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${formatNumber(count)} ${count === 1 ? singular : plural}`;
}

export function projectionObservedInstances(
  projection: InterventionProjection | null | undefined,
): number | null {
  const basis = projection?.basis;
  if (!basis) return null;
  if (isPresentNumber(basis.observedInstances)) return basis.observedInstances;
  if (isPresentNumber(basis.observedPopulation)) return basis.observedPopulation;
  const sampleSize = projection.bandWidthInputs?.sampleSize;
  return isPresentNumber(sampleSize) ? sampleSize : null;
}

export function projectionObservationWindowDays(
  projection: InterventionProjection | null | undefined,
): number | null {
  const basis = projection?.basis;
  if (!basis) return null;
  const explicit = basis.observationWindowDays;
  if (isPresentNumber(explicit)) return explicit;
  if (isPresentNumber(basis.baselineWindowDays)) return basis.baselineWindowDays;
  return isPresentNumber(projection?.observationHorizonDays)
    ? projection.observationHorizonDays
    : null;
}

export function isThinProjectionEvidence(
  projection: InterventionProjection | null | undefined,
): boolean {
  if (!projection) return false;
  const basisThin = projection.basis?.thinEvidence;
  if (typeof basisThin === 'boolean') return basisThin;
  return Boolean(projection.bandWidthInputs?.thinEvidence);
}

export function projectionEvidenceStrength(
  projection: InterventionProjection | null | undefined,
): string {
  const explicit = projection?.basis?.evidenceStrength?.trim().toLowerCase();
  if (explicit === 'thin') return 'Thin Evidence';
  if (explicit === 'strong') return 'Strong Evidence';
  if (explicit) return labelize(explicit);
  return isThinProjectionEvidence(projection) ? 'Thin Evidence' : 'Strong Evidence';
}

export function projectionBasisItems(
  projection: InterventionProjection | null | undefined,
): ProjectionBasisItem[] {
  const basis = projection?.basis;
  if (!projection || !basis) return [];

  const observedInstances = projectionObservedInstances(projection);
  const observationWindowDays = projectionObservationWindowDays(projection);
  const baselineValue = basis.baselineValue ?? basis.baselineMean;
  const signalUsed = basis.signalUsed ?? projection.movementSignal;

  return [
    {
      id: 'observed-instances',
      label: 'Observed Instances',
      value: isPresentNumber(observedInstances)
        ? pluralize(observedInstances, 'instance')
        : 'Not available',
      detail: basis.instanceSignal ?? undefined,
    },
    {
      id: 'observation-window',
      label: 'Observation Window',
      value: isPresentNumber(observationWindowDays)
        ? `${formatNumber(observationWindowDays)} days`
        : 'Not available',
      detail:
        isPresentNumber(projection.observationHorizonDays) &&
        projection.observationHorizonDays !== observationWindowDays
          ? `${projection.observationHorizonDays}-day projection horizon`
          : undefined,
    },
    {
      id: 'baseline-value',
      label: 'Baseline Value',
      value: formatMetricValue(baselineValue, signalUsed?.unit),
      detail: basis.signalKey ?? undefined,
    },
    {
      id: 'signal-used',
      label: 'Signal Used',
      value: signalUsed?.conceptLabel?.trim() || labelize(signalUsed?.concept),
      detail: signalUsed?.signalName ?? undefined,
    },
    {
      id: 'corroboration-status',
      label: 'Corroboration Status',
      value: labelize(basis.corroborationStatus),
      detail:
        basis.corroborationSources && basis.corroborationSources.length > 0
          ? basis.corroborationSources.join(', ')
          : undefined,
    },
    {
      id: 'evidence-strength',
      label: 'Evidence Strength',
      value: projectionEvidenceStrength(projection),
      detail: projection.bandWidthInputs
        ? `${labelize(projection.bandWidthInputs.sampleTier)} sample, ${labelize(
            projection.bandWidthInputs.recurrenceStability,
          )} recurrence`
        : undefined,
    },
  ];
}

export function projectionBasisSummary(
  projection: InterventionProjection | null | undefined,
): string | null {
  if (!projection?.basis) return null;

  const observed = projectionObservedInstances(projection);
  const windowDays = projectionObservationWindowDays(projection);
  const baselineValue = projection.basis.baselineValue ?? projection.basis.baselineMean;
  const signalUsed = projection.basis.signalUsed ?? projection.movementSignal;
  const parts: string[] = [];

  if (isPresentNumber(observed)) {
    parts.push(`${pluralize(observed, 'observed instance')}`);
  }
  if (isPresentNumber(windowDays)) {
    parts.push(`${formatNumber(windowDays)}-day window`);
  }
  if (isPresentNumber(baselineValue)) {
    parts.push(`baseline ${formatMetricValue(baselineValue, signalUsed?.unit)}`);
  }
  if (signalUsed?.conceptLabel?.trim()) {
    parts.push(`signal ${signalUsed.conceptLabel}`);
  }
  parts.push(labelize(projection.basis.corroborationStatus).toLowerCase());
  parts.push(projectionEvidenceStrength(projection).toLowerCase());

  return parts.length > 0 ? `Basis: ${parts.join(' | ')}` : null;
}

export function ProjectionThinEvidenceNotice({
  projection,
  compact = false,
}: {
  projection: InterventionProjection | null | undefined;
  compact?: boolean;
}) {
  if (!isThinProjectionEvidence(projection)) return null;

  return (
    <div
      data-testid="projection-thin-evidence-warning"
      role="status"
      className={`rounded-md border border-amber-500/40 bg-amber-500/10 text-amber-700 ${
        compact ? 'px-2 py-1 text-[11px]' : 'px-3 py-2 text-xs'
      }`}
    >
      <span className="font-semibold">Thin evidence</span>
      {' - '}
      projection band is wider because evidence is limited.
    </div>
  );
}

export function ProjectionBasisCompact({
  projection,
  showTitle = true,
}: {
  projection: InterventionProjection | null | undefined;
  showTitle?: boolean;
}) {
  const summary = projectionBasisSummary(projection);
  if (!summary) return null;

  return (
    <div
      data-testid="projection-basis-compact"
      className="rounded-md border border-border/70 bg-bg/30 px-3 py-2"
    >
      {showTitle && (
        <div className="text-xs font-semibold text-text">Projection Basis</div>
      )}
      <p className="mt-1 text-xs leading-relaxed text-muted">{summary}</p>
      <div className="mt-2">
        <ProjectionThinEvidenceNotice projection={projection} compact />
      </div>
    </div>
  );
}

export default function ProjectionBasisPanel({
  projection,
}: {
  projection: InterventionProjection | null | undefined;
}) {
  const items = projectionBasisItems(projection);
  if (items.length === 0) return null;

  return (
    <div data-testid="projection-basis-panel">
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Projection Basis</span>
        <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
          {projectionEvidenceStrength(projection)}
        </span>
      </div>
      <div className="space-y-3 rounded-lg border border-border bg-bg/30 p-3">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {items.map((item) => (
            <div
              key={item.id}
              className="min-w-0 rounded-md border border-border/70 bg-panel/70 px-3 py-2"
            >
              <div className="text-[11px] font-semibold uppercase text-muted">
                {item.label}
              </div>
              <div className="mt-1 break-words text-xs font-semibold leading-snug text-text">
                {item.value}
              </div>
              {item.detail && (
                <div className="mt-1 break-words font-mono text-[11px] leading-tight text-muted">
                  {item.detail}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
