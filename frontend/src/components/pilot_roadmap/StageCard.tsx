import React from 'react';
import { OpportunityCandidate } from '../../types/analystReview';
import { RoadmapStage } from '../../types/pilotRoadmap';
import ReadinessPill from './ReadinessPill';

// ─── Commented out with the "Required Data Permissions" / "Dependencies"
//     sections below (hidden per request). Uncomment together to restore. ───
// import { useState } from 'react';
// import { ChevronRight } from 'lucide-react';
// import { PermissionItem } from '../../types/analystReview';
// import { Readiness, RoadmapDependency } from '../../types/pilotRoadmap';
// import { readinessFromPermission, stageReadiness } from '../../utils/buildRoadmap';

interface Props {
  stage: RoadmapStage;
  onOpenReview: (id: string) => void;
  renderBlueprintLink?: (oppId: string) => React.ReactNode;
}

// ─── Helpers for the hidden "Required Data Permissions" / "Dependencies"
//     sections. Uncomment together with those sections to restore. ───
// function permRowStyle(p: PermissionItem) {
//   const status = readinessFromPermission(p);
//   const cls =
//     status === 'READY'
//       ? 'roadmap-permission-ready border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
//       : status === 'PENDING'
//         ? 'roadmap-permission-pending border-amber-500/30 bg-amber-500/10 text-amber-200'
//         : 'roadmap-permission-missing border-red-500/30 bg-red-500/10 text-red-200';
//   return { status, cls };
// }
//
// function countsFromStatuses<T extends { status: Readiness }>(items: T[]) {
//   return items.reduce(
//     (acc, item) => {
//       if (item.status === 'READY') acc.ready++;
//       if (item.status === 'PENDING') acc.pending++;
//       if (item.status === 'MISSING') acc.missing++;
//       return acc;
//     },
//     { ready: 0, pending: 0, missing: 0 },
//   );
// }
//
// function ReadinessCounts({
//   ready,
//   pending,
//   missing,
// }: {
//   ready: number;
//   pending: number;
//   missing: number;
// }) {
//   return (
//     <span className="flex flex-wrap items-center justify-end gap-x-1 gap-y-0.5 text-[10px]">
//       <span className="roadmap-count-ready whitespace-nowrap font-semibold text-emerald-300">{ready} READY</span>
//       <span className="opacity-10">&middot;</span>
//       <span className="roadmap-count-pending whitespace-nowrap font-semibold text-amber-300">{pending} PENDING</span>
//       <span className="opacity-10">&middot;</span>
//       <span className="roadmap-count-missing whitespace-nowrap font-semibold text-red-300">{missing} MISSING</span>
//     </span>
//   );
// }

export default function StageCard({ stage, onOpenReview, renderBlueprintLink }: Props) {
  // ─── State/derived values for the hidden sections below. Uncomment to restore. ───
  // const [showDependencies, setShowDependencies] = useState(false);
  //
  // const required = stage.requiredPermissions.filter((p) => p.required);
  // const readyCount = stage.requiredPermissions.filter((p) => readinessFromPermission(p) === 'READY').length;
  // const pendingCount = stage.requiredPermissions.filter((p) => readinessFromPermission(p) === 'PENDING').length;
  // const missingCount = required.filter((p) => readinessFromPermission(p) === 'MISSING').length;
  //
  // const dependencyCounts = countsFromStatuses(stage.dependencies);
  //
  // const hasPermScroll = stage.requiredPermissions.length > 4;
  // const hasDepsScroll = stage.dependencies.length > 3;
  //
  // const permScrollStyle = {
  //   height: '180px',
  // };
  // const depsScrollStyle = {
  //   height: '148px',
  // };

  // Simplified gate: READY when the stage has opportunities, otherwise MISSING.
  // Original permission-driven gate (restore with the commented sections below):
  // const gate = stage.opportunities.length === 0 ? 'MISSING' : stageReadiness(stage.requiredPermissions);
  const gate = stage.opportunities.length === 0 ? 'MISSING' : 'READY';

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-panel p-4">
      <div className="flex shrink-0 items-center justify-between">
        <div className="text-xl font-semibold text-text">Stage Readiness</div>
        <ReadinessPill status={gate} />
      </div>

      <div className="opp-scroll mt-4 min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
        <div className="rounded-lg border border-border bg-bg/20 p-3">
          <div className="text-sm font-semibold text-text">Selected Opportunities</div>
          <div className="mt-2 space-y-2">
            {stage.opportunities.length === 0 && (
              <div className="text-sm text-muted">No opportunities assigned to this stage yet.</div>
            )}
            {stage.opportunities.map((o: OpportunityCandidate) => (
              <button
                key={o.id}
                className="roadmap-light-shadow-item w-full rounded-md border border-border bg-bg/20 px-3 py-2 text-left hover:bg-panel2"
                onClick={() => onOpenReview(o.id)}
                data-testid={`opp-row-${o.id}`}
              >
                <div className="text-sm font-semibold text-text">{o.title}</div>
                <div className="mt-1 flex flex-col gap-0.5 text-xs text-muted">
                  <span>{o.category}</span>
                  <span>Tier {o.tier}</span>
                  <span>Confidence {o.confidence}</span>
                </div>
                {renderBlueprintLink?.(o.id)}
              </button>
            ))}
          </div>
        </div>

        {/* ─── "Required Data Permissions" section — hidden per request.
             Uncomment (with the imports/helpers/derived values above) to restore:
        <div className="rounded-lg border border-border bg-bg/20 p-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
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
                <div key={i} className={`roadmap-light-shadow-item rounded-md border px-3 py-2 text-sm ${cls}`}>
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
        ─── */}

        {/* ─── "Dependencies" section — hidden per request.
             Uncomment (with the imports/helpers/derived values above) to restore:
        <div className="rounded-lg border border-border bg-bg/20 p-3">
          <button
            className="flex w-full flex-wrap items-start justify-between gap-3 text-left"
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
        ─── */}
      </div>
    </div>
  );
}
