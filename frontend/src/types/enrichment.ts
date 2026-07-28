// Stage 2 entity summary — surfaced in the evidence trace below BaselineContextPanel.
// resolution_status='ambiguous' signals the UI to render with muted styling.
export interface EntitySummary {
  entity_id: string;
  entity_type: string;
  display_name: string;
  source_system: string;
  resolution_confidence: number;
  resolution_status: 'resolved' | 'ambiguous';
  // Optional defensive field. The backend already filters OppEnrichment entities
  // by entity_min_run_count, but the UI also hides early entities if a payload carries it.
  run_count?: number | null;
}

// Stage 2 relationship edge (T3-S13-A) — surfaced in the evidence trace.
// Shape mirrors the backend RelationshipSummary exactly (snake_case).
// `inferred` is load-bearing: when true the UI must prefix the relationship
// description with the [inferred] label and use `confidence` as the numeric
// signal. Observed edges (inferred=false) are graph truth; inferred edges are
// co-firing hypotheses and only appear when INFERRED_RELATIONSHIPS_ENABLED.
export interface RelationshipSummary {
  from_entity_name: string;
  from_entity_type: string;
  relationship_type: string;
  to_entity_name: string;
  to_entity_type: string;
  inferred: boolean;
  confidence: number;
}

// ENT-6 / T3-S16-A causal chain hypothesis — surfaced in the evidence trace.
// Shape mirrors the backend CausalHypothesisSummary exactly (snake_case), like
// the entities/relationships/temporal fields. All six fields are always present
// in the response. `preliminary`/`preliminary_reason` are load-bearing: T9
// branches on them — preliminary=true renders the amber "analyst review
// required" banner with preliminary_reason; preliminary=false renders the
// confirmed cause chain. `inferred` marks chains containing inferred steps.
export interface CausalHypothesisSummary {
  cause_chain: string[];
  falsifiability_condition: string;
  confidence: number;
  inferred: boolean;
  preliminary: boolean;
  preliminary_reason: string | null;
}

export interface OppEnrichment {
  oppId: string;
  aiSummary: string;
  aiWhyBullets: string[];
  aiRisks: string[];
  aiSuggestedNextSteps: string[];
  llmGenerated: boolean;
  llmModel: string | null;
  // Temporal fields use snake_case to match the backend JSON response directly.
  // This is an intentional exception to the camelCase frontend convention.
  baseline_context: string | null;
  trend_direction: string | null;
  anomaly_score: number | null;
  is_anomalous: boolean;
  first_deviation: boolean;
  baseline_mean: number | null;
  baseline_stddev?: number | null;
  baseline_window_days?: number | null;
  run_count: number | null;
  current_value?: number | null;
  recent_values?: number[];
  signal_key?: string | null;
  pack_id?: string | null;
  // Stage 2 entity list — empty array/null when no entities extracted yet.
  entities: EntitySummary[] | null;
  // Backend-owned display threshold for entity summaries.
  entity_min_run_count?: number | null;
  // Stage 2 relationship edges — empty array by default. Observed edges only
  // unless INFERRED_RELATIONSHIPS_ENABLED is set on the backend, in which case
  // inferred edges (inferred=true) are also included.
  relationships: RelationshipSummary[];
  // ENT-6 / T3-S16-A — causal chain hypothesis, loaded live from the
  // causal_hypotheses table (most-recent row). Optional/null: absence is
  // distinct from an empty hypothesis. null/undefined -> omit the section;
  // preliminary=true -> amber banner; preliminary=false -> full rendering.
  causal_hypothesis?: CausalHypothesisSummary | null;
  // ENT-2 — Cross-System Confidence Elevation. snake_case to match backend JSON.
  // Safe defaults from the backend: empty arrays / false / null.
  // corroboration_label is shared with ENT-3 (carried through from ENT-2).
  corroboration_sources?: string[];
  corroboration_label?: string | null;
  triple_corroboration?: boolean;
  corroboration_rule_ids?: string[];
  // ENT-3 / T3-S15-A — LLM enrichment enterprise hardening (snake_case to match
  // the backend JSON directly, like the temporal/entity fields above).
  // Graph grounding: whether the first pass ran against the ENT-4 graph, and
  // the entity counts behind the 15-entity cap.
  llm_grounded: boolean;
  graph_entity_count: number;
  graph_entity_count_shown: number;
  graph_truncated: boolean;
  // Hallucination guard outcomes for this opportunity.
  hallucination_removals: string[];
  hallucination_rewrites: number;
  hallucination_llm_rewrites: number;
  // Preliminary quality gate: when true the evidence trace shows an
  // "Analyst review required" banner with preliminary_reason.
  preliminary: boolean;
  preliminary_reason: string | null;
  // 2.0-A1 — intervention projection, read from the stored opportunity.
  // null/undefined -> omit the panel (the detector has no signal profile, or the
  // finding carries too few measured instances to project).
  projection?: InterventionProjection | null;
}

/**
 * 2.0-A1 — the magnitude band. Always a RANGE, never a point estimate:
 * lowPct is always strictly below highPct.
 */
export interface MagnitudeBand {
  lowPct: number;
  highPct: number;
  /** What the percentages are a share OF, e.g. "of the recurring instances". */
  basisUnit: string;
  /** Pre-rendered range label, e.g. "25–55% of the recurring instances". */
  label: string;
}

/** 2.0-A1 — a measured signal the projection expects to move. */
export interface ProjectedSignal {
  /** One of: queue_volume | ageing | recurrence_count | time_to_resolve | reassignment_hops */
  concept: string;
  conceptLabel: string;
  /** The real detector field name, e.g. "owner_changes_90d". */
  signalName: string;
  /** count | days | hours | ratio | pct */
  unit: string;
  currentValue: number | null;
  /** "decrease" | "increase" — which way is an improvement. */
  directionOfImprovement: string;
}

/**
 * 2.0-A1 — a per-opportunity intervention projection.
 *
 * A projection is a DIRECTION and a MAGNITUDE BAND on specific measured signals
 * — never a point estimate, never a guaranteed saving. Render the band as a
 * range and the basis alongside it; do not reduce it to a single number.
 */
export interface InterventionProjection {
  schemaVersion: string;
  /** "improves" | "no_material_change" */
  direction: string;
  /** null when direction is "no_material_change". */
  magnitudeBand: MagnitudeBand | null;
  /** 30 | 60 | 90 — the window over which movement would be observable. */
  observationHorizonDays: number;
  /** The manual step the agent is expected to replace. */
  manualStepReplaced: string;
  /** The signal that should move if the agent is implemented. */
  movementSignal: ProjectedSignal;
  affectedSignals: ProjectedSignal[];
  /** What the projection was computed from — render this with the band. */
  basis: {
    detectorId: string;
    observedInstances: number | null;
    observedPopulation: number | null;
    instanceSignal: string | null;
    populationSignal: string | null;
    baselineMean: number | null;
    baselineStddev: number | null;
    baselineWindowDays: number | null;
    observedRunCount: number | null;
    signalKey?: string | null;
    confidence: string | null;
    /** triple | corroborated | supporting_only | single_source */
    corroborationStatus: string;
    corroborationSources: string[];
    packId?: string | null;
    packVersion?: string | null;
    evidenceIds: string[];
  };
  /** Why the band is as wide as it is — deterministic, not a hand-set number. */
  bandWidthInputs: {
    /** strong | moderate | thin | minimal */
    sampleTier: string;
    sampleSize: number | null;
    /** steady | variable | bursty | unknown */
    recurrenceStability: string;
    corroborationStatus: string;
    /** True when the band is visibly wider because the evidence is thin. */
    thinEvidence: boolean;
  };
  /** True when the finding's confidence is capped for want of corroboration. */
  confidenceCapped: boolean;
}

export interface RunEnrichment {
  runId: string;
  executiveSummary: string;
  opportunitiesEnriched: number;
  opportunitiesFailed: number;
  generatedAt: string | null;
  llmModel: string | null;
  available: boolean;
}
