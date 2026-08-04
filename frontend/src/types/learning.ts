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
