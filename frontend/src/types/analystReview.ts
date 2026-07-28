import type { Decision, Confidence } from "./common";

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
  // R191-P1 T3 — the pack (and its version at run time) that produced this
  // finding. Optional/additive: a single-pack run stamps its one pack; a
  // multi-pack run stamps whichever pack's detector fired. Absent on runs
  // materialized before this field existed. Also present on opportunities
  // embedded in PilotRoadmap stages (RoadmapStage.opportunities reuses this
  // same type), satisfying "every roadmap entry carries its packId".
  packId?: string;
  packVersion?: string;
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
