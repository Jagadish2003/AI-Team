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
  run_count: number | null;
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
