import React from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, Map } from 'lucide-react';
import { useRunContext } from '../../context/RunContext';
 
interface Stage {
  opportunities: unknown[];
  requiredPermissions: { required: boolean; satisfied: boolean; label: string }[];
}
 
interface PilotRoadmapHighlightsProps {
  stages: Stage[];
  blockerCount: number;
  overallReadiness: string;
}
 
export default function PilotRoadmapHighlights({
  stages,
  blockerCount,
  overallReadiness,
}: PilotRoadmapHighlightsProps) {
  const nav = useNavigate();
  const { runId } = useRunContext();
 
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="flex items-center gap-2 text-sm font-semibold text-text">
        <Map className="h-4 w-4 shrink-0" />
        <span>Pilot Roadmap Highlights</span>
      </div>
      <ul className="mt-3 space-y-2">
        <li className="flex items-start gap-2 text-sm text-text">
          <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
          <span>{stages[0].opportunities.length} opportunities planned for the next 30 days.</span>
        </li>
        <li className="flex items-start gap-2 text-sm text-text">
          <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
          <span>{stages[1].opportunities.length} opportunities planned for the next 60 days.</span>
        </li>
        <li className="flex items-start gap-2 text-sm text-text">
          <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
          <span>{stages[2].opportunities.length} opportunities planned for the next 90 days.</span>
        </li>
        {blockerCount > 0 && (
          <li className="flex items-start gap-2 text-sm text-text">
            <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
            <span>
              {blockerCount} required data permission{blockerCount > 1 ? 's' : ''} still missing — resolve
              before pilots start.
            </span>
          </li>
        )}
        <li className="flex items-start gap-2 text-sm text-text">
          <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
          <span>
            Overall readiness: <span className="font-semibold">{overallReadiness}</span>.
          </span>
        </li>
      </ul>
      <button
        className="mt-3 w-full rounded-md border border-border bg-bg/20 px-3 py-2 text-sm hover:bg-panel2"
        onClick={() => nav(runId ? `/pilot-roadmap?runId=${runId}` : '/pilot-roadmap')}
      >
        Open Pilot Roadmap
      </button>
    </div>
  );
}
