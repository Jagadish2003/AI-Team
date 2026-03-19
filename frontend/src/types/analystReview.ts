export type Confidence = 'LOW' | 'MEDIUM' | 'HIGH';
export type Decision = 'UNREVIEWED' | 'APPROVED' | 'REJECTED';
export type OpportunityTier = 'Quick Win' | 'Strategic' | 'Complex';

export interface PermissionItem {
  label: string;
  required: boolean;
  satisfied: boolean;
}

export interface EvidenceItem {
  id: string;
  label: string;
}

export interface OpportunityCandidate {
  id: string;
  identifier: string;
  title: string;
  category: string;
  tier: OpportunityTier;
  impact: number;
  effort: number;
  confidence: Confidence;
  aiRationale: string;
  evidenceIds: string[];
  evidenceItems: EvidenceItem[];
  permissions: PermissionItem[];
  decision: Decision;
  override: {
    isLocked: boolean;
    rationaleOverride: string;
    overrideReason: string;
    updatedAt: string | null;
  };
}

export interface ReviewAuditEvent {
  id: string;
  tsLabel: string;
  action: string;
  by: string;
}
