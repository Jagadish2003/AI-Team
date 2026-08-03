export type HealthSeverity = "critical" | "high" | "medium" | "low";
export type HealthPanelId = "connectors" | "runs" | "content" | "packs";

export interface ConnectorHealthItem {
  connector_id: string;
  name: string;
  tier?: string | null;
  connection_state: string;
  auth_mode?: string | null;
  last_successful_ingestion?: string | null;
  checkpoint_position?: string | null;
  checkpoint_captured_at?: string | null;
  checkpoint_age_seconds?: number | null;
  last_error?: string | null;
}

export interface ConnectorHealthResponse {
  org_id: string;
  connectors: ConnectorHealthItem[];
}

export interface DegradedStage {
  stage: string;
  reason: string;
}

export interface StageOutcome {
  stage?: string | null;
  level?: string | null;
  message?: string | null;
}

export interface RunHealthItem {
  run_id: string;
  status: string;
  health_status: string;
  degraded?: boolean;
  started_at?: string | null;
  updated_at?: string | null;
  duration_seconds?: number | null;
  systems?: string[];
  system_count?: number | null;
  pack_id?: string | null;
  detectors_evaluated?: number | null;
  detectors_fired?: number | null;
  opportunities?: number | null;
  degraded_stages?: DegradedStage[];
  stage_outcomes?: StageOutcome[];
}

export interface RunHealthResponse {
  org_id: string;
  runs: RunHealthItem[];
}

export interface IndexedSourceHealth {
  source_system: string;
  chunk_count: number;
  embedded_count: number;
}

export interface BackfillHealth {
  active_model?: string | null;
  embedded_total?: number;
  on_active_model?: number;
  awaiting_backfill?: number;
  progress?: number;
  complete?: boolean;
}

export interface SkippedHealthItem {
  reason: string;
  count: number;
}

export interface ContentHealthResponse {
  org_id: string;
  generated_at: string;
  indexed_by_source: IndexedSourceHealth[];
  chunks_total: number;
  chunks_embedded: number;
  pending_embeddings: number;
  stale_chunks: number;
  pending_change_events: number;
  failed_refreshes: number;
  backfill: BackfillHealth;
  redaction_count: number;
  skipped: SkippedHealthItem[];
}

/** 2.0-C1 T2 (AT-827): a pack's lifecycle state for the organisation. */
export type PackLifecycleState = "active" | "disabled";

export interface PackHealthItem {
  pack_id: string;
  pack_name?: string | null;
  /**
   * The version that ACTUALLY executed for this run — an immutable historical
   * fact. For a rolled-back run this is the pinned version, not the version the
   * registry currently ships.
   */
  pack_version?: string | null;
  detector_count: number;
  detectors?: string[];
  evaluated_count?: number | null;
  not_evaluated_count?: number | null;
  executed_at?: string | null;
  /**
   * 2.0-C1 T2 (AT-827): the pack's state TODAY. Unlike every other field here —
   * which comes from immutable run fields so a later pack change cannot rewrite
   * what the dashboard says executed — this one is read LIVE, because "is this
   * pack still running?" is a question about now, not about the run. A pack
   * disabled after this run reads "disabled" while its execution record stands.
   */
  pack_state?: PackLifecycleState | null;
  /**
   * 2.0-C1 T3 (AT-828): the version this RUN was pinned to, when it was rolled
   * back. Equal to `pack_version` for a pinned run — this field is what says the
   * version was a deliberate rollback rather than the shipped default.
   */
  pinned_version?: string | null;
  /** True when this run executed a rolled-back (pinned) version. */
  rolled_back?: boolean;
  // 2.0-C2 T3 (AT-833 / AC2): the pack's certification level, read LIVE like
  // pack_state (a badge that stops verifying stops being shown everywhere at once)
  // rather than frozen with the run's immutable execution fields.
  certification_level?: "certified" | "partner" | "community";
  certification_label?: string;
  certification_review_due?: boolean;
  // 2.0-C2 T5 (AT-835): why it is due, and the date it falls due.
  certification_review_due_detail?: string | null;
  certification_review_due_on?: string | null;
  /**
   * 2.0-C4 T2 (AT-843 / AC1): the pack is DEPRECATED today. Read LIVE for the same
   * reason `pack_state` and the certification level are — "is this pack still
   * supported, and until when" is a question about now. Absent (not `false`) for a
   * pack that is not deprecated, so the panel renders a notice or nothing.
   */
  deprecated?: boolean;
  deprecation_phase?: "grace" | "grace_expired";
  deprecation_label?: string;
  deprecation_reason?: string;
  deprecation_on?: string;
  /** The date support ends. Null when no removal date has been announced. */
  deprecation_ends_on?: string | null;
  deprecation_days_remaining?: number | null;
  deprecation_replacement_pack_id?: string | null;
  deprecation_replacement_label?: string | null;
  deprecation_notice?: string;
}

/**
 * 2.0-C1 T2 (AT-827): a pack the run selected that did NOT execute because the
 * organisation has it disabled. Surfaced so an analyst seeing fewer packs than
 * were selected gets the reason instead of an unexplained gap.
 */
export interface ExcludedPackItem {
  packId: string;
  state?: string;
  reason?: string;
}

export interface PackHealthResponse {
  run_id?: string | null;
  packs: PackHealthItem[];
  /** Additive (AT-827). Absent on responses served before the field existed. */
  excluded_packs?: ExcludedPackItem[];
  /** Additive (AT-828): `{ packId: pinnedVersion }` for this run. */
  pinned_pack_versions?: Record<string, string>;
}

export interface AttentionItem {
  id: string;
  condition: string;
  severity: HealthSeverity;
  title: string;
  explanation: string;
  connector_id?: string | null;
  run_id?: string | null;
  timestamp: string;
  panel: HealthPanelId;
  href: string;
  details: Record<string, unknown>;
}

export interface AttentionHealthResponse {
  org_id: string;
  severity_order: HealthSeverity[];
  items: AttentionItem[];
}
