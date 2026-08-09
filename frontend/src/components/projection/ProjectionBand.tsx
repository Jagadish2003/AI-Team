import React from 'react';
import type {
  InterventionProjection,
  ProjectionBandWidth,
  ProjectionBandWidthDriver,
  ProjectionStrength,
} from '../../types/enrichment';

/**
 * 2.0-A1 T4 — the resulting band and its evidence label.
 *
 * Band width is computed on the backend from four evidence inputs (sample size,
 * recurrence stability, corroboration status, confidence cap status) and is
 * never a hand-set number. Nothing here recomputes a width — this module reads
 * what the projection carries and renders it, so a screen can never disagree
 * with the stored projection an analyst is auditing.
 *
 * The one rule with teeth on this side is AC4: wherever projection strength is
 * shown or used for ordering, a capped (single-source) finding is labelled and
 * never presents above a corroborated equivalent on strength alone.
 */

function isPresentNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

export function projectionBandWidth(
  projection: InterventionProjection | null | undefined,
): ProjectionBandWidth | null {
  const bandWidth = projection?.bandWidth;
  return bandWidth ?? null;
}

export function projectionStrength(
  projection: InterventionProjection | null | undefined,
): ProjectionStrength | null {
  return projection?.projectionStrength ?? null;
}

/** The band's range label, e.g. "23–57% of the recurring instances". */
export function projectionBandLabel(
  projection: InterventionProjection | null | undefined,
): string | null {
  const band = projection?.magnitudeBand;
  if (!band) return null;
  if (band.label?.trim()) return band.label;
  return `${band.lowPct}–${band.highPct}% ${band.basisUnit ?? ''}`.trim();
}

/**
 * The evidence label to render beside the band.
 *
 * Prefers the band-width derivation (T4), falls back to the basis mirror, and
 * finally to the pre-T4 thin/strong flag so an older stored projection still
 * renders a truthful label rather than nothing.
 */
export function projectionEvidenceLabel(
  projection: InterventionProjection | null | undefined,
): string | null {
  if (!projection) return null;
  const fromBandWidth = projection.bandWidth?.evidenceLabel?.trim();
  if (fromBandWidth) return fromBandWidth;
  const fromBasis = projection.basis?.evidenceLabel?.trim();
  if (fromBasis) return fromBasis;
  const strength = projection.basis?.evidenceStrength?.trim().toLowerCase();
  if (strength === 'thin') return 'Thin evidence';
  if (strength === 'strong') return 'Strong evidence';
  return null;
}

/** The band-width tier label, e.g. "Wide band". */
export function projectionBandTierLabel(
  projection: InterventionProjection | null | undefined,
): string | null {
  return (
    projection?.bandWidth?.bandLabel?.trim() ||
    projection?.basis?.bandLabel?.trim() ||
    null
  );
}

export function isCappedProjection(
  projection: InterventionProjection | null | undefined,
): boolean {
  if (!projection) return false;
  const strength = projection.projectionStrength;
  if (strength && typeof strength.capped === 'boolean') return strength.capped;
  if (typeof projection.confidenceCapped === 'boolean') return projection.confidenceCapped;
  if (typeof projection.bandWidth?.confidenceCapped === 'boolean') {
    return projection.bandWidth.confidenceCapped;
  }
  if (typeof projection.bandWidthInputs?.confidenceCapped === 'boolean') {
    return projection.bandWidthInputs.confidenceCapped;
  }
  const bandInputStatus = projection.bandWidthInputs?.corroborationStatus
    ?.trim()
    .toLowerCase();
  const basisStatus = projection.basis?.corroborationStatus?.trim().toLowerCase();
  return bandInputStatus === 'single_source' || basisStatus === 'single_source';
}

/** The label a capped projection must carry wherever its strength is shown. */
export function cappedProjectionLabel(
  projection: InterventionProjection | null | undefined,
): string | null {
  if (!isCappedProjection(projection)) return null;
  return (
    projection?.projectionStrength?.cappedLabel?.trim() ||
    'Capped — single-source confidence'
  );
}

/**
 * AC4 ordering key: capped projections sort after uncapped ones, and within a
 * group stronger sorts first.
 *
 * The leading element is what enforces AC4 structurally — a capped finding can
 * never out-rank a corroborated equivalent however large its strength scalar.
 * A projection with no band has no strength and sorts last within its group.
 */
export function projectionRankKey(
  projection: InterventionProjection | null | undefined,
): [number, number] {
  const capped = isCappedProjection(projection) ? 1 : 0;
  const value = projection?.projectionStrength?.value;
  return [capped, isPresentNumber(value) ? -value : 1];
}

/**
 * Stable strongest-first ordering. Items with equal keys keep their incoming
 * order, so this narrows an existing ranking rather than replacing it.
 */
export function orderByProjectionStrength<T>(
  items: readonly T[],
  projectionOf: (item: T) => InterventionProjection | null | undefined,
): T[] {
  return items
    .map((item, index) => ({ item, index, key: projectionRankKey(projectionOf(item)) }))
    .sort((a, b) => a.key[0] - b.key[0] || a.key[1] - b.key[1] || a.index - b.index)
    .map((entry) => entry.item);
}

/**
 * The conservative half of {@link orderByProjectionStrength}, and the mirror of
 * the backend's `demote_capped_projections`.
 *
 * Applies AC4's rule — a capped finding never presents above a corroborated
 * equivalent — WITHOUT letting the strength scalar re-rank anything else. Use
 * this wherever the incoming order already encodes a deliberate decision (the
 * roadmap's stage order, for instance): the sort is stable, so everything but
 * the capped demotion is preserved.
 */
export function demoteCappedProjections<T>(
  items: readonly T[],
  projectionOf: (item: T) => InterventionProjection | null | undefined,
): T[] {
  return items
    .map((item, index) => ({
      item,
      index,
      capped: isCappedProjection(projectionOf(item)) ? 1 : 0,
    }))
    .sort((a, b) => a.capped - b.capped || a.index - b.index)
    .map((entry) => entry.item);
}

function driverRow(driver: ProjectionBandWidthDriver) {
  return (
    <div
      key={driver.axis}
      data-testid={`band-width-driver-${driver.axis}`}
      className="border-b border-border/50 py-1.5 last:border-b-0"
    >
      <div className="min-w-0">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">
          {driver.label}
        </div>
        <div className="mt-0.5 break-words text-xs text-text">{driver.value}</div>
      </div>
    </div>
  );
}

/**
 * The capped-confidence label. Rendered wherever projection strength is shown,
 * so a reader never sees a strength number without the caveat that qualifies it.
 */
export function ProjectionCappedNotice({
  projection,
  compact = false,
}: {
  projection: InterventionProjection | null | undefined;
  compact?: boolean;
}) {
  const label = cappedProjectionLabel(projection);
  if (!label) return null;

  return (
    <div
      data-testid="projection-capped-label"
      role="status"
      className={`rounded-md border border-amber-500/40 bg-amber-500/10 text-amber-700 ${
        compact ? 'px-2 py-1 text-[11px]' : 'px-3 py-2 text-xs'
      }`}
    >
      <span className="font-semibold">{label}</span>
      {' - '}
      not ranked above a corroborated finding on projection strength.
    </div>
  );
}

/**
 * Projection strength, with its cap label. Deliberately shows the tier rather
 * than leading with the raw scalar: the number is for ordering, the tier is
 * what a reader should act on.
 */
export function ProjectionStrengthBadge({
  projection,
}: {
  projection: InterventionProjection | null | undefined;
}) {
  const strength = projectionStrength(projection);
  if (!strength) return null;

  const capped = isCappedProjection(projection);
  return (
    <div data-testid="projection-strength" className="flex flex-wrap items-center gap-2">
      <span
        className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
          capped
            ? 'border-amber-500/50 bg-amber-500/10 text-amber-700'
            : 'border-border bg-bg/30 text-text'
        }`}
      >
        {strength.label}
      </span>
    </div>
  );
}

/**
 * The Opportunity Review surface: the resulting band, its width tier, its
 * evidence label, and the per-axis derivation that produced it.
 */
export default function ProjectionBandPanel({
  projection,
}: {
  projection: InterventionProjection | null | undefined;
}) {
  const bandLabel = projectionBandLabel(projection);
  const evidenceLabel = projectionEvidenceLabel(projection);
  const bandWidth = projectionBandWidth(projection);

  // No band at all (direction "no material change") is a real, honest state —
  // say so rather than rendering an empty panel that reads as missing data.
  if (!bandLabel) {
    if (!projection) return null;
    return (
      <div data-testid="projection-band-panel" className="space-y-2">
        <div className="text-xs font-semibold text-text">Projection Band</div>
        <div className="rounded-lg border border-border bg-bg/30 px-3 py-2 text-xs text-muted">
          No material change projected — the observed evidence is below the
          threshold for a magnitude band.
        </div>
      </div>
    );
  }

  return (
    <div data-testid="projection-band-panel" className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Projection Band</span>
        {evidenceLabel && (
          <span
            data-testid="projection-evidence-label"
            className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text"
          >
            {evidenceLabel}
          </span>
        )}
      </div>

      <div className="space-y-3 rounded-lg border border-border bg-bg/30 p-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div
            data-testid="projection-band-range"
            className="text-sm font-semibold text-text"
          >
            {bandLabel}
          </div>
          {bandWidth && (
            <span
              data-testid="projection-band-tier"
              className="shrink-0 rounded-full border border-border bg-panel/70 px-2 py-0.5 text-[11px] font-semibold text-muted"
            >
              {bandWidth.bandLabel}
            </span>
          )}
        </div>

        <ProjectionCappedNotice projection={projection} />

        {bandWidth && (
          <>
            <p
              data-testid="projection-band-rationale"
              className="text-xs leading-relaxed text-muted"
            >
              {bandWidth.rationale}
            </p>
            <div className="rounded-md border border-border/70 bg-panel/70 px-3 py-2">
              <div className="text-[11px] font-semibold uppercase text-muted">
                What set this width
              </div>
              <div className="mt-1">{bandWidth.drivers.map(driverRow)}</div>
            </div>
            <ProjectionStrengthBadge projection={projection} />
          </>
        )}
      </div>
    </div>
  );
}

/** Compact band + evidence label for the Agentforce Blueprint surface. */
export function ProjectionBandCompact({
  projection,
}: {
  projection: InterventionProjection | null | undefined;
}) {
  const bandLabel = projectionBandLabel(projection);
  if (!bandLabel) return null;
  const evidenceLabel = projectionEvidenceLabel(projection);
  const tierLabel = projectionBandTierLabel(projection);

  return (
    <div
      data-testid="projection-band-compact"
      className="rounded-md border border-border/70 bg-bg/30 px-3 py-2"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span
          data-testid="projection-band-range"
          className="text-sm font-semibold text-text"
        >
          {bandLabel}
        </span>
        {tierLabel && (
          <span
            data-testid="projection-band-tier"
            className="shrink-0 rounded-full border border-border bg-panel/70 px-2 py-0.5 text-[11px] font-semibold text-muted"
          >
            {tierLabel}
          </span>
        )}
      </div>
      {evidenceLabel && (
        <div
          data-testid="projection-evidence-label"
          className="mt-1 text-[11px] text-muted"
        >
          {evidenceLabel}
        </div>
      )}
    </div>
  );
}
