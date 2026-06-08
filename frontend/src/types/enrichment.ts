// Stage 2 entity summary — surfaced in the evidence trace below BaselineContextPanel.
// resolution_status='ambiguous' signals the UI to render with muted styling.
export interface EntitySummary {
  entity_id: string;
  entity_type: string;
  display_name: string;
  source_system: string;
  resolution_confidence: number;
  resolution_status: 'resolved' | 'ambiguous';
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
  // Stage 2 entity list — empty array when no entities extracted yet.
  entities: EntitySummary[];
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
