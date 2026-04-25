
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
