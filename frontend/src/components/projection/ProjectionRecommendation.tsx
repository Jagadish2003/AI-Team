import React from 'react';
import type { InterventionProjection, ProjectionRecommendation } from '../../types/enrichment';

/**
 * 2.0-A1 T5 — recommendation text in intervention language.
 *
 * "This agent will reduce cost by 40%" is a lie generator. The honest form names
 * what the agent handles, which recurring cases are in scope, what stays manual,
 * which measured signal should move, and the band and horizon — no guarantees,
 * no point estimates.
 *
 * The text is composed on the BACKEND and travels on the projection, so this
 * module only renders it. Nothing here writes recommendation copy: a screen that
 * composed its own sentence would be one more place a savings claim could
 * appear, and the vocabulary guard only covers what the backend emits.
 */

export function projectionRecommendation(
  projection: InterventionProjection | null | undefined,
): ProjectionRecommendation | null {
  return projection?.recommendation ?? null;
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
