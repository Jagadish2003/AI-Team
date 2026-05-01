/**
 * T41-5 v1.1 — StagesGrid.tsx
 *
 * Changes from v1.0:
 *   Issue 1 fix: Blueprint link now passed as a renderBlueprintLink render
 *     prop to StageCard. StageCard renders the link INSIDE each opportunity
 *     row — card-bound, visually associated with the specific opportunity.
 *
 *   StageCardWithBlueprint wrapper removed — it was the source of the
 *   detached-link problem. StageCard is called directly with the prop.
 *
 *   Issue 2 (documentation): source key registry note added as comment.
 *
 * Phase 1/2/3 labels and subtitles unchanged from v1.0.
 */
import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Zap } from 'lucide-react';
import { RoadmapStage } from '../../types/pilotRoadmap';
import StageCard from './StageCard';
import { useConnectorContext } from '../../context/ConnectorContext';
import { useAnalystReviewContext } from '../../context/AnalystReviewContext';

// ── Phase label mapping ───────────────────────────────────────────────────────

const PHASE_LABELS: Record<string, { phase: string; description: string }> = {
  NEXT_30: { phase: 'Phase 1', description: 'Starter Agents' },
  NEXT_60: { phase: 'Phase 2', description: 'Connected Agents' },
  NEXT_90: { phase: 'Phase 3', description: 'Orchestrated Agents' },
};

function phaseLabel(stageId: string) {
  return PHASE_LABELS[stageId] ?? { phase: stageId, description: '' };
}

function phaseSubtitle(stage: RoadmapStage): string {
  const count = stage.opportunities.length;
  if (count === 0) return 'No opportunities assigned';
  const names = stage.opportunities
    .slice(0, 2)
    .map((o) => o.title.split(' ').slice(0, 3).join(' '));
  return count === 1
    ? names[0]
    : `${names.join(', ')}${count > 2 ? ` +${count - 2} more` : ''}`;
}

// ── Blueprint link — rendered inside each opportunity row ─────────────────────

function BlueprintLink({ oppId }: { oppId: string }) {
  const nav = useNavigate();
  const { select } = useAnalystReviewContext();

  return (
    <button
      onClick={(e) => {
        // Stop propagation so the outer opportunity button's onOpenReview
        // does not also fire when the Blueprint link is clicked.
        e.stopPropagation();
        select(oppId);
        nav(`/agentforce-blueprint?oppId=${encodeURIComponent(oppId)}`);
      }}
      className="flex items-center gap-1 text-[10px] text-accent hover:underline mt-1.5"
      data-testid={`blueprint-link-${oppId}`}
      aria-label={`View Agentforce Blueprint for this opportunity`}
    >
      <Zap size={9} />
      View Agentforce Blueprint →
    </button>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface Props {
  stages: RoadmapStage[];
  onOpenReview: (id: string) => void;
}

export default function StagesGrid({ stages, onOpenReview }: Props) {
  const { all: connectors } = useConnectorContext();

  // Issue 2 (documentation): 'salesforce' must match sourceKeys.ts SOURCE_KEY_MAP
  // connector ID — stable string defined in the codebase.
  const salesforceConnected = connectors.some(
    (c) => c.id === 'salesforce' && c.status === 'connected',
  );

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      {stages.map((s) => {
        const { phase, description } = phaseLabel(s.id);
        const subtitle = phaseSubtitle(s);

        return (
          <div key={s.id} className="space-y-2">
            {/* Phase heading replacing 30/60/90 */}
            <div className="text-center" data-testid={`phase-heading-${s.id}`}>
              <div className="text-sm font-bold uppercase tracking-wide text-muted">
                {phase} — {description}
              </div>
              <div className="text-xs text-muted/70 mt-0.5 truncate px-2">
                {subtitle}
              </div>
            </div>

            {/* Issue 1 fix: pass renderBlueprintLink as render prop.
                StageCard calls it with each oppId and renders the result
                INSIDE the opportunity row button — card-bound. */}
            <StageCard
              stage={s}
              onOpenReview={onOpenReview}
              renderBlueprintLink={
                salesforceConnected
                  ? (oppId) => <BlueprintLink oppId={oppId} />
                  : undefined
              }
            />
          </div>
        );
      })}
    </div>
  );
}
