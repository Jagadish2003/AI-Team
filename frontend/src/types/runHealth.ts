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
  /**
   * Number of per-stream checkpoints the reported age represents, when the
   * connector checkpoints per stream ({connector_id}:{stream}) rather than under
   * its bare id. Absent/null for a single-cursor connector. The age is that of the
   * NEWEST stream — see health_aggregation._read_stream_checkpoint.
   */
  checkpoint_streams?: number | null;
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

export interface PackHealthItem {
  pack_id: string;
  pack_name?: string | null;
  pack_version?: string | null;
  detector_count: number;
  detectors?: string[];
  evaluated_count?: number | null;
  not_evaluated_count?: number | null;
  executed_at?: string | null;
}

export interface PackHealthResponse {
  run_id?: string | null;
  packs: PackHealthItem[];
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
