/**
 * Sprint 4 T7 — LLM Enrichment API client
 *
 * Calls the two T6 endpoints:
 *   GET /api/runs/{runId}/llm-enrichment
 *   GET /api/runs/{runId}/opportunities/{oppId}/enrichment
 *
 * Both endpoints always return usable data:
 *   - available: false  → enrichment not yet generated (show template text)
 *   - llmGenerated: false → fallback mode (aiSummary = aiRationale template)
 *   - llmGenerated: true  → Claude-generated content
 */
import { apiGet } from '../lib/apiClient';

export interface OppEnrichment {
  oppId: string;
  aiSummary: string;
  aiWhyBullets: string[];
  aiRisks: string[];
  aiSuggestedNextSteps: string[];
  llmGenerated: boolean;
  llmModel: string | null;
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

export async function fetchOppEnrichment(
  runId: string,
  oppId: string
): Promise<OppEnrichment> {
  return apiGet<OppEnrichment>(
    `/api/runs/${runId}/opportunities/${oppId}/enrichment`
  );
}

export async function fetchRunEnrichment(runId: string): Promise<RunEnrichment> {
  return apiGet<RunEnrichment>(`/api/runs/${runId}/llm-enrichment`);
}
