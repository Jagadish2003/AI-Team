import React from 'react';
import { useState } from 'react';
import { RoadmapStage, RoadmapDependency, Readiness } from '../../types/pilotRoadmap';
import { OpportunityCandidate, PermissionItem } from '../../types/analystReview';
import { readinessFromPermission, stageReadiness } from '../../utils/buildRoadmap';
import ReadinessPill from './ReadinessPill';
import { ChevronRight } from 'lucide-react';

interface Props {
  stage: RoadmapStage;
  onOpenReview: (id: string) => void;
}

function permRowStyle(p: PermissionItem) {
  const status = readinessFromPermission(p);
  const cls =
    status === 'READY'   ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200' :
    status === 'PENDING' ? 'border-amber-500/30  bg-amber-500/10  text-amber-200'   :
                           'border-red-500/30    bg-red-500/10    text-red-200';
  return { status, cls };
}

function countsFromStatuses<T extends { status: Readiness }>(items: T[]) {
  return items.reduce(
    (acc, item) => {
      if (item.status === 'READY') acc.ready++;
      if (item.status === 'PENDING') acc.pending++;
      if (item.status === 'MISSING') acc.missing++;
      return acc;
    },
    { ready: 0, pending: 0, missing: 0 }
  );
}
function ReadinessCounts({ ready, pending, missing }: { ready: number; pending: number; missing: number }) {
  return (
    <span className="text-xs flex items-center gap-1">
      <span className="font-semibold text-emerald-300">{ready} READY</span>
      <span className="opacity-10">·</span>
      <span className="font-semibold text-amber-300">{pending} PENDING</span>
      <span className="opacity-10">·</span>
      <span className="font-semibold text-red-300">{missing} MISSING</span>
    </span>
  );
}

export default function StageCard({ stage, onOpenReview }: Props) {
  const [showDependencies, setShowDependencies] = useState(false);

  const required = stage.requiredPermissions.filter(p => p.required);
  const readyCount = stage.requiredPermissions.filter(
    p => readinessFromPermission(p) === 'READY'
  ).length;
  const pendingCount = stage.requiredPermissions.filter(
    p => readinessFromPermission(p) === 'PENDING'
  ).length;
  const missingCount = required.filter(
    p => readinessFromPermission(p) === 'MISSING'
  ).length;

  const gate = stageReadiness(stage.requiredPermissions);
  const dependencyCounts = countsFromStatuses(stage.dependencies);

  const hasPermScroll = stage.requiredPermissions.length > 4;
  const hasDepsScroll = stage.dependencies.length > 3;

  const permScrollStyle = {
    height: '180px',
    scrollbarWidth: 'thin' as const,
    scrollbarColor: '#9e9fa3 #132043',
  };
  const depsScrollStyle = {
    height: '148px',
    scrollbarWidth: 'thin' as const,
    scrollbarColor: '#9e9fa3 #132043',
  };

  return (
    <div className="rounded-xl border border-border bg-panel p-4 flex flex-col h-full">
      <style>{`
        .opp-scroll::-webkit-scrollbar { width: 6px; }
        .opp-scroll::-webkit-scrollbar-track { background: #132043; border-radius: 6px; }
        .opp-scroll::-webkit-scrollbar-thumb { background: #9e9fa3; border-radius: 6px; min-height: 40px; }
        .opp-scroll::-webkit-scrollbar-thumb:hover { background: #c0c1c5; }
        .opp-scroll::-webkit-scrollbar-button:start { background: #132043; height: 10px; display: block; }
        .opp-scroll::-webkit-scrollbar-button:end { background: #132043; height: 10px; display: block; }
      `}</style>
      <div className="flex items-center justify-between">
        <div className="text-xl font-semibold text-text">Stage Readiness</div>
        <ReadinessPill status={gate} />
      </div>

      <div className="mt-4 rounded-lg border border-border bg-bg/20 p-3">
        <div className="text-sm font-semibold text-text">Selected Opportunities</div>
        <div className="mt-2 min-h-[190px] max-h-[190px] space-y-2 overflow-y-auto pr-1">
          {stage.opportunities.length === 0 && (
            <div className="text-sm text-muted">No opportunities assigned to this stage yet.</div>
          )}
          {stage.opportunities.map((o: OpportunityCandidate) => (
            <button
              key={o.id}
              className="w-full rounded-md border border-border bg-bg/20 px-3 py-2 text-left hover:bg-panel2"
              onClick={() => onOpenReview(o.id)}
            >
              <div className="text-sm font-semibold text-text">{o.title}</div>
              <div className="mt-1 text-xs text-muted">
                {o.category} · Tier {o.tier} · Confidence {o.confidence}
              </div>
            </button>
          ))}
        </div>
      </div>
      <div className="mt-3 rounded-lg border border-border bg-bg/20 p-3">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold text-text">Required Data Permissions</div>
          <ReadinessCounts ready={readyCount} pending={pendingCount} missing={missingCount} />
        </div>
        <div
          className={hasPermScroll ? 'opp-scroll mt-2 space-y-2 overflow-y-scroll pr-1' : 'mt-2 space-y-2'}
          style={hasPermScroll ? permScrollStyle : {}}
        >
          {stage.requiredPermissions.map((p: PermissionItem, i: number) => {
            const { status, cls } = permRowStyle(p);
            return (
              <div key={i} className={`rounded-md border px-3 py-2 text-sm ${cls}`}>
                <div className="flex items-center justify-between">
                  <span>{p.label}</span>
                  <ReadinessPill status={status} />
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-2 text-xs text-muted">
          Required permissions drive gate readiness. Recommended permissions influence quality and confidence.
        </div>
      </div>
      <div className="mt-3 rounded-lg border border-border bg-bg/20 p-3">
        <button
          className="flex w-full items-start justify-between gap-3 text-left"
          onClick={() => setShowDependencies(!showDependencies)}
        >
          <span className="flex items-center gap-2 text-sm font-semibold text-text">
            <ChevronRight
              size={16}
              className={`transition-transform duration-200 ${showDependencies ? 'rotate-90' : ''}`}
            />
            Dependencies
          </span>
          <ReadinessCounts
            ready={dependencyCounts.ready}
            pending={dependencyCounts.pending}
            missing={dependencyCounts.missing}
          />
        </button>
        {showDependencies && (
          <div
            className={hasDepsScroll ? 'opp-scroll mt-2 space-y-2 overflow-y-scroll pr-1' : 'mt-2 space-y-2'}
            style={hasDepsScroll ? depsScrollStyle : {}}
          >
            {stage.dependencies.map((d: RoadmapDependency) => (
              <div
                key={d.id}
                className="flex items-center justify-between rounded-md border border-border bg-bg/10 px-3 py-2 text-sm"
              >
                <span className="text-text">{d.label}</span>
                <ReadinessPill status={d.status} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}