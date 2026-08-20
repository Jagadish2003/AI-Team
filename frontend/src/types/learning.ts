export interface LearningActivationCounts {
  weightedSignals: number;
  decisions: number;
  outcomes: number;
  distinctIdentities: number;
}

export interface LearningActivationThresholds {
  minimumDecisions: number;
  minimumSignals: number;
  minimumDistinctIdentities: number;
}

export interface LearningActivationRemaining {
  decisions: number;
  weightedSignals: number;
  distinctIdentities: number;
}

export interface LearningActivationState {
  status: "active" | "learning_not_yet_active";
  isActive: boolean;
  message: string | null;
  currentCount: number;
  threshold: number;
  counts: LearningActivationCounts;
  thresholds: LearningActivationThresholds;
  remaining: LearningActivationRemaining;
  basis?: string;
  policy?: string;
}

export interface LearningSignalSetResponse {
  schemaVersion: string;
  orgId: string;
  configVersion: string;
  collectedAt: string;
  isActive: boolean;
  inactiveReason: string | null;
  counts: {
    total: number;
    weighted: number;
    outcomes: number;
    decisions: number;
    distinctIdentities: number;
  };
  thresholds: {
    minimumDecisions: number;
    minimumSignals: number;
    minimumDistinctIdentities: number;
  };
  activation: LearningActivationState;
}

export interface LearningAdjustmentCaps {
  maxScoreFraction: number;
  maxRankMove: number;
  pointsPerSignalUnit: number;
}

export interface LearningAdjustmentGroup {
  detectorId: string | null;
  packId: string | null;
  signalConcept: string | null;
  netWeight: number;
  outcomeWeight: number;
  decisionWeight: number;
  hasOutcomeEvidence: boolean;
  signalCount: number;
  learningActive: boolean;
  contributingRefs: unknown[];
  configVersion: string | null;
  revision: number;
  computedAt: string | null;
  updatedAt: string | null;
}

export interface LearningAdjustmentStateResponse {
  orgId: string;
  enabled: boolean;
  caps: LearningAdjustmentCaps;
  configVersion: string;
  learningState: LearningActivationState;
  groups: LearningAdjustmentGroup[];
}

export type LearningAdjustmentChangeKind =
  | 'activated'
  | 'recomputed'
  | 'deactivated'
  | 'reset';

export interface LearningAdjustmentHistoryEntry {
  schemaVersion: string;
  historyId: string;
  orgId: string;
  detectorId: string | null;
  packId: string | null;
  changeKind: LearningAdjustmentChangeKind;
  previousNetWeight: number | null;
  netWeight: number;
  signalCount: number;
  learningActive: boolean;
  actorId: string;
  configVersion: string | null;
  revision: number;
  recordedAt: string;
  resetReason?: string | null;
  resetMarker?: boolean;
}

export interface LearningAdjustmentResetResponse {
  schemaVersion: string;
  orgId: string;
  changeKind: 'reset';
  groupsReset: number;
  opportunitiesAffected: number;
  previousState: LearningAdjustmentGroup[];
  currentState: LearningAdjustmentGroup[];
  configVersion: string;
  resetAt: string;
  actorId: string;
  reason: string;
}
