
import React from 'react';
import { ArrowRight, Lightbulb } from 'lucide-react';
import { fetchRunEnrichment, RunEnrichment } from '../../api/enrichmentApi';
import { useRunContext } from '../../context/RunContext';
import { useResource } from '../../lib/dataCache';
import { cacheKeys } from '../../lib/cacheKeys';

export const STATIC_SUMMARY =
  'AgentIQ identified high-ROI "agentic moments" from operational signals ' +
  '(tickets + systems of record). Start with 2\u20133 quick wins in the next 30 days, ' +
  'prove measurable cycle-time reduction, then expand evidence coverage and ' +
  'productionize governance in the 60\u201390 day window.';

/** The "What leadership should do next" checklist. Shared with the PDF export
 *  so the on-screen card and the downloaded report never drift apart.
 *
 *  TODO(executive-report): these action items are currently static placeholders.
 *  Make them data-driven (derive from run results — e.g. quick-win count,
 *  outstanding permission blockers, readiness) or serve them from the run-scoped
 *  executive-report API so they reflect the actual run instead of fixed copy. */
export const LEADERSHIP_ACTIONS = [
  'Approve top quick wins and confirm success metrics.',
  'Grant required permissions for 30-day pilots (read-only first).',
  'Assign an executive sponsor and implementation owner per pilot.',
  'Schedule a 2-week checkpoint with evidence and governance sign-off.',
];

/** Resolve the executive summary shown in Key Insights: the LLM summary when
 *  available, otherwise the deterministic static fallback. Mirrored by the PDF. */
export function resolveExecutiveSummary(enrichment: RunEnrichment | null): string {
  return enrichment?.available && enrichment.executiveSummary
    ? enrichment.executiveSummary
    : STATIC_SUMMARY;
}

export default function KeyInsights() {
  const { runId } = useRunContext();
  // Shared with ExecutiveReportPage on cacheKeys.runEnrichment → one fetch, not
  // two. Fail-open: an undefined result (not loaded / errored) uses the static
  // fallback via resolveExecutiveSummary.
  const { data } = useResource<RunEnrichment>(
    runId ? cacheKeys.runEnrichment(runId) : null,
    () => fetchRunEnrichment(runId as string),
  );
  const summary = resolveExecutiveSummary(data ?? null);

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-semibold text-text">Key Insights</div>
      </div>

      {/* Executive summary — LLM or static fallback */}
      <div className="text-sm text-text leading-relaxed">
        {summary}
      </div>

      {/* What leadership should do next — static, always shown */}
      <div className="mt-4 rounded-lg border border-border bg-bg/20 p-3">
        <div className="flex items-center gap-2 text-xs font-semibold text-text">
          <Lightbulb className="h-4 w-4 shrink-0" />
          <span>What leadership should do next</span>
        </div>
        <ul className="mt-2 space-y-2">
          {LEADERSHIP_ACTIONS.map((action) => (
            <li key={action} className="flex items-start gap-2 text-sm text-text">
              <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
              <span>{action}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
