import React from 'react';
import { OpportunityCandidate } from '../../types/analystReview';
import { RoadmapStage } from '../../types/pilotRoadmap';
import {
  ProjectionStrengthBadge,
  demoteCappedProjections,
  projectionBandLabel,
} from '../projection/ProjectionBand';
import ReadinessPill from './ReadinessPill';
import { showRelease2ArcAUi } from '../../config/releaseFlags';
import { RankingAdjustmentCompact } from '../learning/RankingAdjustment';

interface Props {
  stage: RoadmapStage;
  onOpenReview: (id: string) => void;
  renderBlueprintLink?: (oppId: string) => React.ReactNode;
}

// R18-C0 P6: the Roadmap page shows phases only. The stage card is focused on
// selected opportunities and rollout progression — the "Required Data
// Permissions" and "Dependencies" blocks were removed so the customer-facing
// roadmap tells the implementation story rather than acting as a permissions
// debugger. Permission readiness still lives on the Agentforce Blueprint.
export default function StageCard({ stage, onOpenReview, renderBlueprintLink }: Props) {
  // Gate: READY when the stage has opportunities, otherwise MISSING.
  const gate = stage.opportunities.length === 0 ? 'MISSING' : 'READY';

  // 2.0-A1 T4 / AC4 — projection strength, used carefully.
  //
  // The backend already applied the same rule when it built the roadmap, so
  // this is a display-side guarantee rather than a re-ranking: a capped
  // (single-source) finding never presents above a corroborated equivalent,
  // whatever order reaches this component. The sort is stable, so the stage's
  // existing order — which encodes analyst decisions — is otherwise preserved.
  const opportunities = showRelease2ArcAUi
    ? demoteCappedProjections(
        stage.opportunities,
        (opp) => opp.projection,
      )
    : stage.opportunities;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-panel p-4">
      <div className="flex shrink-0 items-center justify-between">
        <div className="text-base font-semibold text-text">Stage Readiness</div>
        <ReadinessPill status={gate} />
      </div>

      <div className="opp-scroll mt-4 min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
        <div className="rounded-lg border border-border bg-bg/20 p-3">
          <div className="text-sm font-semibold text-text">Selected Opportunities</div>
          <div className="mt-2 space-y-2">
            {opportunities.length === 0 && (
              <div className="text-sm text-muted">No opportunities assigned to this stage yet.</div>
            )}
            {opportunities.map((o: OpportunityCandidate) => {
              const bandLabel = showRelease2ArcAUi ? projectionBandLabel(o.projection) : null;
              return (
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
                    {/* 2.0-A1 T4 — the band, shown as a band. Never a point
                        estimate and never a savings figure. */}
                    {bandLabel && (
                      <span data-testid={`opp-band-${o.id}`}>Projected {bandLabel}</span>
                    )}
                  </div>
                  {/* Projection strength carries its own cap label, so a reader
                      never sees the ordering signal without its caveat. */}
                  {showRelease2ArcAUi && (
                    <div className="mt-1.5">
                      <ProjectionStrengthBadge projection={o.projection} />
                    </div>
                  )}
                  <RankingAdjustmentCompact ranking={o._ranking} />
                  {renderBlueprintLink?.(o.id)}
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
