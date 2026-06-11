/**
 * CorroborationBadge — ENT-2 Cross-System Confidence Elevation (T5)
 *
 * Renders the cross-system corroboration pill on the opportunity card, between
 * the confidence badge and the rest of the card. Makes "why is this HIGH
 * confidence?" visible without digging into the evidence trace.
 *
 * Display modes (ENT-2 Section 3):
 *   - Triple corroboration (tripleCorroboration=true):
 *       GOLD pill "Triple corroboration" — the enterprise demo moment.
 *   - Corroborated (sources present, not Slack-only):
 *       GREEN pill "Corroborated by N system(s)".
 *   - Slack supporting only (the only source is Slack supporting-only):
 *       MUTED grey pill "Supporting: Slack" — Slack never elevates alone.
 *   - No corroboration (sources empty):
 *       Renders NOTHING. A single-source finding must not look supported.
 *
 * Tooltip: native title attribute listing the corroboration sources (the
 * codebase uses title-based tooltips elsewhere, e.g. OpportunityDetail).
 *
 * Styling matches the existing badge conventions in OpportunityList.tsx
 * (ConfidenceBadge / DecisionBadge): rounded-full border pills.
 */
import React from 'react';

export interface CorroborationBadgeProps {
  /** Human-readable source labels, e.g. ['ServiceNow', 'Jira']. */
  sources?: string[];
  /** Display label from the engine, e.g. 'Corroborated by ServiceNow incidents'. */
  label?: string | null;
  /** True when Salesforce + ServiceNow + Jira all corroborate. */
  tripleCorroboration?: boolean;
}

/** A source is "Slack supporting only" when its label marks it as supporting. */
function isSlackSupportingOnly(sources: string[]): boolean {
  if (sources.length === 0) return false;
  return sources.every((s) => /supporting only/i.test(s));
}

export default function CorroborationBadge({
  sources,
  label,
  tripleCorroboration = false,
}: CorroborationBadgeProps) {
  const list = Array.isArray(sources) ? sources.filter(Boolean) : [];
  const labelText = typeof label === 'string' ? label.trim() : '';

  // No corroboration → render nothing (single-source findings stay clean).
  if (list.length === 0) {
    if (labelText) {
      return (
        <span
          data-testid="corroboration-badge"
          data-variant="incomplete"
          title={labelText}
          className="inline-flex items-center gap-1 rounded-full border border-border bg-bg/30 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-muted whitespace-nowrap"
        >
          <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-muted" />
          Corroboration details unavailable
        </span>
      );
    }
    return null;
  }

  const base =
    'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide whitespace-nowrap';

  // Gold pill — triple corroboration is the strongest signal.
  if (tripleCorroboration) {
    const tripleTitle = labelText
      ? `${labelText} - ${list.join(', ')}`
      : `Triple corroboration: Salesforce + ServiceNow + Jira - ${list.join(', ')}`;
    return (
      <span
        data-testid="corroboration-badge"
        data-variant="triple"
        title={tripleTitle}
        className={`${base} border-amber-400/60 bg-amber-400/15 text-amber-200`}
      >
        <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-amber-300" />
        Triple corroboration
      </span>
    );
  }

  // Muted pill — Slack supporting only (never elevates confidence).
  if (isSlackSupportingOnly(list)) {
    return (
      <span
        data-testid="corroboration-badge"
        data-variant="supporting"
        title={labelText || `Supporting signal — ${list.join(', ')}`}
        className={`${base} border-border bg-bg/30 text-muted`}
      >
        <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-muted" />
        Supporting: Slack
      </span>
    );
  }

  // Green pill — corroborated by one or more systems.
  const count = list.length;
  const text =
    labelText || (count > 1 ? `Corroborated by ${count} systems` : `Corroborated by ${count} system`);
  return (
    <span
      data-testid="corroboration-badge"
      data-variant="corroborated"
      title={labelText ? `${labelText} — ${list.join(', ')}` : `Corroborated by ${list.join(', ')}`}
      className={`${base} border-emerald-500/50 bg-emerald-500/15 text-emerald-300`}
    >
      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-emerald-300" />
      {text}
    </span>
  );
}
