import { apiGet, apiPost } from "../lib/apiClient";
import type { EvidenceReview } from "../types/evidence";
import type { Decision } from "../types/common";

export function fetchEvidence(runId: string): Promise<EvidenceReview[]> {
  return apiGet<EvidenceReview[]>(`/api/runs/${runId}/evidence`);
}

export function postEvidenceDecision(
  runId: string,
  evidenceId: string,
  decision: Decision
): Promise<EvidenceReview> {
  return apiPost<EvidenceReview>(
    `/api/runs/${runId}/evidence/${evidenceId}/decision`,
    { decision }
  );
}
