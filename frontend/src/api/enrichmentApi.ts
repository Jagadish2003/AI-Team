import { apiGet } from '../lib/apiClient';
import type { OppEnrichment, RunEnrichment } from '../types/enrichment';

export type { OppEnrichment, RunEnrichment } from '../types/enrichment';

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
