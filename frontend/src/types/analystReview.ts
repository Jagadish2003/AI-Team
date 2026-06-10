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
}

export interface ReviewAuditEvent {
  id: string;
  tsLabel: string;
  action: string;
  by: string;
  opportunityId?: string;
}
