export type OutcomeComparabilityVerdict =
  | 'comparable'
  | 'weakly_comparable'
  | 'not_comparable';

export type OutcomeProjectionVerdict =
  | 'within_band'
  | 'above_band'
  | 'below_band'
  | 'not_projected'
  | 'too_early';

export type OpportunityLifecycleState =
  | 'open'
  | 'actioned'
  | 'monitoring'
  | 'measured'
  | 'dismissed'
  | 'stalled';

export interface OpportunityLifecycle {
  orgId: string;
  opportunityIdentity: string;
  state: OpportunityLifecycleState | string;
  actionDate?: string | null;
  actionedBy?: string | null;
  actionedAt?: string | null;
  revision?: number;
  firstSeenRunId?: string | null;
  lastRunId?: string | null;
  lastTransitionAt?: string | null;
  updatedBy?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  measurable?: boolean;
  legalNextStates?: string[];
}

export interface OutcomeConfounderSummary {
  count: number;
  materialCount: number;
  advisoryCount?: number;
  byType?: Record<string, number>;
  types?: string[];
}

export interface OutcomeConfounder {
  schemaVersion?: string;
  type?: string;
  severity?: 'material' | 'advisory' | string;
  label?: string;
  detail?: {
    implication?: string;
    description?: string;
    message?: string;
    reason?: string;
    [key: string]: unknown;
  };
  detectedAt?: string | null;
  thresholdBasis?: string | null;
}

export interface OutcomeNumberEvidence {
  opportunityIdentity?: string | null;
  signalName?: string | null;
  baselineRunId?: string | null;
  currentRunId?: string | null;
  postActionRunIds?: string[];
  baseline?: {
    runId?: string | null;
    value?: number | null;
    window?: Record<string, unknown>;
  };
  current?: {
    runId?: string | null;
    value?: number | null;
    window?: Record<string, unknown>;
  };
  comparability?: Record<string, unknown>;
  confounderSummary?: OutcomeConfounderSummary;
  projectionValidation?: Record<string, unknown>;
  measurementCount?: number;
  runIds?: string[];
  runPairs?: Array<{
    opportunityIdentity?: string | null;
    baselineRunId?: string | null;
    currentRunId?: string | null;
  }>;
  lifecycles?: Array<{
    opportunityIdentity?: string | null;
    state?: string | null;
    actionDate?: string | null;
    lastRunId?: string | null;
  }>;
}

export interface OutcomeNumberRef {
  id: string;
  label: string;
  value: number;
  unit?: 'count' | 'percent' | string | null;
  signalName?: string | null;
  field?: string | null;
  evidence: OutcomeNumberEvidence;
}

export interface OutcomeMeasurement {
  opportunityIdentity?: string | null;
  detectorId?: string | null;
  actionDate?: string | null;
  measuredAt?: string | null;
  baselineRunId?: string | null;
  currentRunId?: string | null;
  primaryMovement?: {
    signalName?: string;
    baselineValue?: number | null;
    currentValue?: number | null;
    delta?: number | null;
    deltaPct?: number | null;
    direction?: string;
    lowerIsBetter?: boolean;
  };
  movements: Array<Record<string, unknown>>;
  comparability: {
    verdict?: OutcomeComparabilityVerdict | string;
    reasons?: string[];
    [key: string]: unknown;
  };
  projectionValidation: {
    verdict?: OutcomeProjectionVerdict | string;
    reason?: string;
    [key: string]: unknown;
  };
  confounderSummary: OutcomeConfounderSummary;
  confounders: OutcomeConfounder[];
  numberRefs: OutcomeNumberRef[];
}

export interface OpportunityOutcomeView {
  schemaVersion: string;
  orgId: string;
  opportunityIdentity: string;
  lifecycle?: OpportunityLifecycle | null;
  measurementCount: number;
  caveatedMeasurementCount: number;
  latestMeasurement?: OutcomeMeasurement | null;
  measurements: OutcomeMeasurement[];
  numberRefs: OutcomeNumberRef[];
  emptyState?: { reason: string; message: string } | null;
}

export interface OutcomeAggregates {
  actionedOpportunityCount: number;
  measuredOpportunityCount: number;
  measurementCount: number;
  caveatedMeasurementCount: number;
  materialCaveatMeasurementCount: number;
  byDirection: Record<string, number>;
  byComparability: Record<string, number>;
  byProjectionValidation: Record<string, number>;
  numberRefs: OutcomeNumberRef[];
}

export interface OutcomePortfolioItem {
  opportunityIdentity: string;
  state?: string | null;
  actionDate?: string | null;
  lastRunId?: string | null;
  measurementCount: number;
  caveatedMeasurementCount: number;
  latestMeasurement?: OutcomeMeasurement | null;
  measurements: OutcomeMeasurement[];
  emptyState?: { reason: string; message: string } | null;
}

export interface OutcomePortfolioView {
  schemaVersion: string;
  orgId: string;
  filters: Record<string, string[]>;
  aggregates: OutcomeAggregates;
  count: number;
  items: OutcomePortfolioItem[];
}

export interface OutcomeReportSection {
  schemaVersion: string;
  runId: string;
  generatedFrom: 'stored_movement_records' | string;
  summary: string;
  aggregates: OutcomeAggregates;
  highlights: OutcomeMeasurement[];
  numberRefs: OutcomeNumberRef[];
}
