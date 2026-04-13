import { apiGet, apiPost } from "../lib/apiClient";
import type { OpportunityCandidate } from "../types/analystReview";
import type { Decision } from "../types/common";

export async function fetchRunOpportunities(runId: string): Promise<OpportunityCandidate[]> {
  return apiGet<OpportunityCandidate[]>(`/api/runs/${runId}/opportunities`);
}

export async function postRunOpportunityDecision(
  runId: string,
  oppId: string,
  decision: Decision
): Promise<OpportunityCandidate> {
  return apiPost<OpportunityCandidate>(
    `/api/runs/${runId}/opportunities/${oppId}/decision`,
    { decision }
  );
}

export async function postRunOpportunityOverride(
  runId: string,
  oppId: string,
  payload: { rationaleOverride: string; overrideReason: string; isLocked: boolean }
): Promise<OpportunityCandidate> {
  return apiPost<OpportunityCandidate>(
    `/api/runs/${runId}/opportunities/${oppId}/override`,
    payload
  );
}
