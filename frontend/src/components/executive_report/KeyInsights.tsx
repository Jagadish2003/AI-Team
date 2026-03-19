import React from 'react';
import { ArrowRight, Lightbulb } from 'lucide-react';
 
export default function KeyInsights() {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-semibold text-text">Key Insights</div>
      <div className="mt-2 text-sm text-text">
        AgentIQ identified high-ROI "agentic moments" from operational signals (tickets + systems of record).
        Start with 2–3 quick wins in the next 30 days, prove measurable cycle-time reduction, then expand
        evidence coverage and productionize governance in the 60–90 day window.
      </div>
 
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