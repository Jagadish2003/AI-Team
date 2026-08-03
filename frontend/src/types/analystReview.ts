import type { Decision, Confidence } from "./common";
import type { InterventionProjection } from "./enrichment";

export type Tier = "Quick Win" | "Strategic" | "Complex";
export type OpportunityTier = Tier; // backward-compat alias

export interface PermissionItem {
  label: string;
  required: boolean;
  satisfied: boolean;
}

export interface EvidenceItem {
  id: string;
  label: string;
}

export interface OpportunityOverride {
  isLocked: boolean;
  rationaleOverride: string;
  overrideReason: string;
  updatedAt: string | null;
}

export interface OpportunityCandidate {
  id: string;
  identifier?: string;             // legacy mock field
  opportunity_identity?: string;    // stable cross-run outcome/lifecycle key
  title: string;
  category: string;
  tier: Tier;
  impact: number;
  effort: number;
  confidence: Confidence;
  aiRationale: string;
  summary?: string;
  evidenceIds: string[];
  evidenceItems?: EvidenceItem[];  // legacy mock field
  requiredPermissions?: string[];  // backend contract field
  permissions?: PermissionItem[];  // legacy mock field
  decision: Decision;
  override: OpportunityOverride;
  // ENT-2 — Cross-System Confidence Elevation (optional; absent on older runs).
  corroboration_sources?: string[];
  corroboration_label?: string | null;
  triple_corroboration?: boolean;
  corroboration_rule_ids?: string[];
  // 2.0-A1 — intervention projection, stored with the opportunity by the run
  // pipeline. Absent on older runs and on findings that are not projectable.
  projection?: InterventionProjection | null;
  // R191-P1 T3 — the pack (and its version at run time) that produced this
  // finding. Optional/additive: a single-pack run stamps its one pack; a
  // multi-pack run stamps whichever pack's detector fired. Absent on runs
  // materialized before this field existed. Also present on opportunities
  // embedded in PilotRoadmap stages (RoadmapStage.opportunities reuses this
  // same type), satisfying "every roadmap entry carries its packId".
  packId?: string;
  packVersion?: string;
  // 2.0-A3 T2 — the bounded learned ranking adjustment (contract v1.18).
  // Additive and optional: absent when learning is off, not yet active, or on
  // responses served before this shipped. Base scoring is NOT affected —
  // `impact`/`effort`/`tier`/`confidence` and all evidence fields are untouched,
  // and `_ranking.baseRank` is what this finding would have ranked without
  // learning, so the "without learning" view needs no second request.
  _ranking?: OpportunityRanking;
}

export interface OpportunityRankingCaps {
  /** Max fraction of base impact a learned adjustment may move. */
  maxScoreFraction: number;
  /** Max positions a finding may move from its base rank, in either direction. */
  maxRankMove: number;
}

export interface OpportunityRanking {
  schemaVersion: string;
  /** Position without learning. The stored order IS the base order. */
  baseRank: number;
  baseImpact: number;
  adjustedRank: number;
  /** Positions moved; negative means it moved up the list. */
  moved: number;
  adjusted: boolean;
  caps: OpportunityRankingCaps;
  // Present only when a learned adjustment applied to this finding.
  effectiveImpact?: number;
  appliedDelta?: number;
  requestedDelta?: number;
  /** True when a cap prevented the full move — learning and the base scorer disagree. */
  wasCapped?: boolean;
  cappedBy?: "score_fraction" | "rank_move" | null;
  hasOutcomeEvidence?: boolean;
  signalCount?: number;
}

export interface ReviewAuditEvent {
  id: string;
  tsLabel: string;
  tsEpoch?: number;
  action: string;
  // P8: the prior decision this event replaced, present on decision-change
  // events so each Approve/Reject transition is explicit in the history.
  previousDecision?: Decision;
  by: string;
  opportunityId?: string;
}
