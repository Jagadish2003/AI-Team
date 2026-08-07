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
  // 2.0-C1 T2 (AT-827) — the producing pack's state TODAY, stamped at serve time.
  // A finding is NEVER removed or rewritten when its pack is disabled; these two
  // additive fields are how a reader can tell that the finding came from a pack
  // that is no longer running. `packVersion` above still reports the version that
  // produced it, so provenance is intact either way. Absent on responses served
  // before the fields existed.
  packState?: "active" | "disabled";
  packStateLabel?: string;
  // 2.0-C2 T3 (AT-833 / AC2): the certification level of the pack that produced
  // this finding, so a board paper quoting it can say which level of pack it came
  // from. Stamped at serve time and always the EFFECTIVE (signature-verified)
  // level — an unverifiable Certified claim arrives as "community" (2.0-C2 AC1).
  packCertificationLevel?: "certified" | "partner" | "community";
  packCertificationLabel?: string;
  // Valid badge, reviewed against an older platform version. Additive, never a
  // downgrade.
  packCertificationReviewDue?: boolean;
  // 2.0-C4 T2 (AT-843 / AC1): the producing pack is being superseded. Stamped at
  // serve time (like packState and the certification level, and unlike the
  // immutable packVersion) and present ONLY for a deprecated pack, so a surface
  // renders a notice or nothing. The finding itself is never altered.
  packDeprecated?: boolean;
  packDeprecationPhase?: "grace" | "grace_expired";
  packDeprecationLabel?: string;
  /** The one-sentence notice: reason, dates, and the replacement. */
  packDeprecationNotice?: string;
  /** The date support ends. Absent when no removal date has been announced. */
  packDeprecationEndsOn?: string;
  /** Absent when no replacement pack has been named. */
  packDeprecationReplacementPackId?: string;
  packDeprecationReplacementLabel?: string;
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
  // 2.0-A3 T3 — why this finding moved (contract v1.19). Absent when it did not
  // move: there is no ordering change to explain, and rendering "not adjusted
  // because..." on every finding would bury the ones that were.
  reason?: AdjustmentReason;
}

/** How much evidence an adjustment rests on. Served as data so the UI can style
 *  the hedge rather than string-matching the summary sentence. */
export type AdjustmentEvidenceStrength =
  | "minimal"
  | "limited"
  | "moderate"
  | "substantial";

export interface ContributingDecisionRef {
  kind: "decision";
  feedbackId: string | null;
  action: string | null;
  opportunityIdentity: string | null;
  reasonCode: string | null;
  actorId: string | null;
  recordedAt: string | null;
  /** Resolves to GET /api/learning/feedback/entry/{feedbackId}. */
  href: string | null;
}

export interface ContributingOutcomeRef {
  kind: "outcome";
  opportunityIdentity: string | null;
  verdict: string | null;
  currentRunId: string | null;
  baselineRunId: string | null;
  measuredDirection: string | null;
  /** Carried so a caveated measurement never presents as a clean one. */
  comparabilityVerdict: string | null;
  measuredAt: string | null;
  /** Resolves to GET /api/opportunity-movement/{opportunityIdentity}. */
  href: string | null;
}

/**
 * Structured, not prose. The `summary` is rendered from these fields by the
 * backend so every surface shows identical wording — never compose your own,
 * and never render this alongside confidence, corroboration or the evidence
 * trace: the adjustment changed ORDER only, and copy placed among those would
 * imply the learned signal contributed to the finding's credibility.
 */
export interface AdjustmentReason {
  schemaVersion: string;
  direction: "up" | "down";
  ranksMoved: number;
  baseRank: number;
  adjustedRank: number;
  decisionCount: number;
  decisionsByAction: Record<string, number>;
  outcomeCount: number;
  outcomesByVerdict: Record<string, number>;
  hasOutcomeEvidence: boolean;
  wasCapped: boolean;
  cappedBy: "score_fraction" | "rank_move" | null;
  evidenceStrength: AdjustmentEvidenceStrength;
  totalSignals: number;
  contributingDecisions: ContributingDecisionRef[];
  contributingOutcomes: ContributingOutcomeRef[];
  /** The rendered sentence. Display this; do not build one from the fields. */
  summary: string;
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
