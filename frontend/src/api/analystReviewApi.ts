import { apiGet, apiPost } from "../lib/apiClient";
import type { OpportunityCandidate, ReviewAuditEvent } from "../types/analystReview";
import type { Decision } from "../types/common";

export function fetchOpportunities(runId: string): Promise<OpportunityCandidate[]> {
  return apiGet<OpportunityCandidate[]>(`/api/runs/${runId}/opportunities`);
}

export function postOpportunityDecision(
  runId: string,
  oppId: string,
  decision: Decision
): Promise<OpportunityCandidate> {
  return apiPost<OpportunityCandidate>(
    `/api/runs/${runId}/opportunities/${oppId}/decision`,
    { decision }
  );
}

export function postOpportunityOverride(
  runId: string,
  oppId: string,
  payload: { rationaleOverride: string; overrideReason: string; isLocked: boolean }
): Promise<OpportunityCandidate> {
  return apiPost<OpportunityCandidate>(
    `/api/runs/${runId}/opportunities/${oppId}/override`,
    payload
  );
}

export function fetchAudit(runId: string): Promise<ReviewAuditEvent[]> {
  return apiGet<ReviewAuditEvent[]>(`/api/runs/${runId}/audit`);
}
