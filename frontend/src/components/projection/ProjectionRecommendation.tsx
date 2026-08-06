import React from 'react';
import type {
  InterventionProjection,
  ProjectionRecommendation,
  ProjectionRecommendationPart,
} from '../../types/enrichment';

/**
 * 2.0-A1 T5 — recommendation text in intervention language.
 *
 * "This agent will reduce cost by 40%" is a lie generator. The honest form names
 * what the agent handles, which recurring cases are in scope, what stays manual,
 * which measured signal should move, and the band and horizon — no guarantees,
 * no point estimates.
 *
 * New projections carry backend-composed copy. Older stored projections may
 * predate that field, so this module has a conservative fallback built only
 * from projection facts and still avoids guaranteed-savings language.
 */

export function projectionRecommendation(
  projection: InterventionProjection | null | undefined,
): ProjectionRecommendation | null {
  return projection?.recommendation ?? fallbackRecommendation(projection);
}

function formatCount(value: number | null | undefined): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(value);
}

function observedScope(projection: InterventionProjection): string {
  const instances = formatCount(
    projection.basis?.observedInstances ?? projection.bandWidthInputs?.sampleSize,
  );
  if (instances) return `${instances} recurring instances`;
  const population = formatCount(projection.basis?.observedPopulation);
  if (population) return `${population} observed records`;
  return 'the identified recurring cases';
}

function signalLabel(projection: InterventionProjection): string {
  return (
    projection.movementSignal?.conceptLabel?.trim() ||
    projection.basis?.signalUsed?.conceptLabel?.trim() ||
    projection.movementSignal?.signalName?.trim() ||
    projection.basis?.signalUsed?.signalName?.trim() ||
    'the measured signal'
  );
}

function fallbackRecommendation(
  projection: InterventionProjection | null | undefined,
): ProjectionRecommendation | null {
  if (!projection) return null;

  const scope = observedScope(projection);
  const manualStep = projection.manualStepReplaced?.trim() || 'the repeated manual step';
  const band = projection.magnitudeBand?.label?.trim();
  const horizon = Number.isFinite(projection.observationHorizonDays)
    ? `${projection.observationHorizonDays}-day horizon`
    : 'the shown observation horizon';
  const signal = signalLabel(projection);

  const parts: ProjectionRecommendationPart[] = [
    {
      id: 'agent_handles',
      label: 'What the agent handles',
      text: `The agent handles ${manualStep}.`,
    },
    {
      id: 'cases_in_scope',
      label: 'Cases in scope',
      text: `In scope: ${scope}.`,
    },
    {
      id: 'remains_manual',
      label: 'What remains manual',
      text: 'Remaining manual: records whose handling does not match a known pattern.',
    },
    {
      id: 'signal_expected_to_move',
      label: 'Signal expected to move',
      text: `The signal expected to move is ${signal}.`,
    },
    {
      id: 'band_and_horizon',
      label: 'Projection band and horizon',
      text: band
        ? `Projected movement is ${band}, observable over about ${horizon}.`
        : `No material movement band is projected over ${horizon}.`,
    },
  ];

  const headline = `Agent handles ${scope}; the residual requires judgement (records whose handling does not match a known pattern).`;
  return {
    schemaVersion: 'ui-fallback',
    headline,
    parts,
    nextSteps: [
      `Confirm with the owning team that ${scope} match the pattern described here.`,
      'Agree the boundary for records whose handling does not match a known pattern before build.',
      `Record the current value of ${signal} as the baseline to re-measure after the agent is live.`,
    ],
    summary: [headline, ...parts.map((part) => part.text)].join(' '),
  };
}

export function recommendationHeadline(
  projection: InterventionProjection | null | undefined,
): string | null {
  const headline = projectionRecommendation(projection)?.headline?.trim();
  return headline || null;
}

/** The flattened one-string form, for surfaces with no room for the parts. */
export function recommendationSummary(
  projection: InterventionProjection | null | undefined,
): string | null {
  const recommendation = projectionRecommendation(projection);
  if (!recommendation) return null;
  const summary = recommendation.summary?.trim();
  if (summary) return summary;
  const parts = (recommendation.parts ?? []).map((p) => p.text).filter(Boolean);
  const composed = [recommendation.headline, ...parts].filter(Boolean).join(' ').trim();
  return composed || null;
}

export function recommendationNextSteps(
  projection: InterventionProjection | null | undefined,
): string[] {
  return projectionRecommendation(projection)?.nextSteps ?? [];
}

/**
 * The headline alone — used where a single line must carry the recommendation
 * (roadmap rows, quick-win cards, PDF card headers).
 */
export function RecommendationHeadline({
  projection,
  className = '',
}: {
  projection: InterventionProjection | null | undefined;
  className?: string;
}) {
  const headline = recommendationHeadline(projection);
  if (!headline) return null;

  return (
    <div
      data-testid="recommendation-headline"
      className={`text-xs leading-relaxed text-text ${className}`}
    >
      {headline}
    </div>
  );
}

/**
 * The full recommendation: headline plus the five named parts.
 *
 * Rendered as labelled rows rather than a paragraph so a reader can see that
 * each required element is actually present — "what remains manual" being
 * visibly absent is the failure mode this layout makes obvious.
 */
export default function ProjectionRecommendationPanel({
  projection,
}: {
  projection: InterventionProjection | null | undefined;
}) {
  const recommendation = projectionRecommendation(projection);
  if (!recommendation) return null;

  const parts = recommendation.parts ?? [];
  const nextSteps = recommendation.nextSteps ?? [];

  return (
    <div data-testid="projection-recommendation-panel" className="space-y-2">
      <div className="text-xs font-semibold text-text">Recommendation</div>

      <div className="space-y-3 rounded-lg border border-border bg-bg/30 p-3">
        <p
          data-testid="recommendation-headline"
          className="text-sm font-semibold leading-relaxed text-text"
        >
          {recommendation.headline}
        </p>

        {parts.length > 0 && (
          <div className="space-y-2">
            {parts.map((part) => (
              <div
                key={part.id}
                data-testid={`recommendation-part-${part.id}`}
                className="min-w-0 rounded-md border border-border/70 bg-panel/70 px-3 py-2"
              >
                <div className="text-[11px] font-semibold uppercase text-muted">
                  {part.label}
                </div>
                <div className="mt-1 break-words text-xs leading-relaxed text-text">
                  {part.text}
                </div>
              </div>
            ))}
          </div>
        )}

        {nextSteps.length > 0 && (
          <div>
            <div className="text-[11px] font-semibold uppercase text-muted">
              Suggested next steps
            </div>
            <ul
              data-testid="recommendation-next-steps"
              className="mt-1 list-disc space-y-1 pl-4 text-xs leading-relaxed text-text"
            >
              {nextSteps.map((step, index) => (
                <li key={`${step}-${index}`}>{step}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

/** Compact form: headline + next steps only. For the Blueprint's purpose block. */
export function ProjectionRecommendationCompact({
  projection,
}: {
  projection: InterventionProjection | null | undefined;
}) {
  const recommendation = projectionRecommendation(projection);
  if (!recommendation) return null;

  return (
    <div
      data-testid="projection-recommendation-compact"
      className="rounded-md border border-border/70 bg-bg/30 px-3 py-2"
    >
      <p
        data-testid="recommendation-headline"
        className="text-xs font-semibold leading-relaxed text-text"
      >
        {recommendation.headline}
      </p>
      {(recommendation.parts ?? []).length > 0 && (
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          {(recommendation.parts ?? [])
            .filter((p) => p.id === 'remains_manual' || p.id === 'signal_expected_to_move')
            .map((p) => p.text)
            .join(' ')}
        </p>
      )}
    </div>
  );
}
