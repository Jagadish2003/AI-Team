
import React, { useEffect, useState } from 'react';
import { ArrowRight, Lightbulb } from 'lucide-react';
import { fetchRunEnrichment, RunEnrichment } from '../../api/enrichmentApi';
import { useRunContext } from '../../context/RunContext';

const STATIC_SUMMARY =
  'AgentIQ identified high-ROI "agentic moments" from operational signals ' +
  '(tickets + systems of record). Start with 2\u20133 quick wins in the next 30 days, ' +
  'prove measurable cycle-time reduction, then expand evidence coverage and ' +
  'productionize governance in the 60\u201390 day window.';

export default function KeyInsights() {
  const { runId } = useRunContext();
  const [enrichment, setEnrichment] = useState<RunEnrichment | null>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    fetchRunEnrichment(runId)
      .then(data => { if (!cancelled) setEnrichment(data); })
      .catch((err) => {
        if (!cancelled) setEnrichment(null);
        console.warn('[T7] KeyInsights enrichment fetch failed:', err);
      });
    return () => { cancelled = true; };
  }, [runId]);

  const llmSummary = enrichment?.available && enrichment.executiveSummary
    ? enrichment.executiveSummary
    : null;

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-semibold text-text">Key Insights</div>
        {llmSummary && (
          <span className="text-xs border border-bg rounded px-1.5 py-0.5 text-text">
            Claude
          </span>
        )}
      </div>

      {/* Executive summary — LLM or static fallback */}
      <div className="text-sm text-text leading-relaxed">
        {llmSummary ?? STATIC_SUMMARY}
      </div>

      {/* What leadership should do next — static, always shown */}
      <div className="mt-4 rounded-lg border border-border bg-bg/20 p-3">
        <div className="flex items-center gap-2 text-xs font-semibold text-text">
          <Lightbulb className="h-4 w-4 shrink-0" />
          <span>What leadership should do next</span>
        </div>
        <ul className="mt-2 space-y-2">
          <li className="flex items-start gap-2 text-sm text-text">
            <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
            <span>Approve the top 2 quick wins and confirm success metrics.</span>
          </li>
          <li className="flex items-start gap-2 text-sm text-text">
            <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
            <span>Grant required permissions for 30-day pilots (read-only first).</span>
          </li>
          <li className="flex items-start gap-2 text-sm text-text">
            <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
            <span>Assign an executive sponsor and implementation owner per pilot.</span>
          </li>
          <li className="flex items-start gap-2 text-sm text-text">
            <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
            <span>Schedule a 2-week checkpoint with evidence and governance sign-off.</span>
          </li>
        </ul>
      </div>
    </div>
  );
}
