import { apiGet } from "../lib/apiClient";
import type { ReviewAuditEvent } from "../types/analystReview";

export async function fetchRunAudit(runId: string): Promise<ReviewAuditEvent[]> {
  return apiGet<ReviewAuditEvent[]>(`/api/runs/${runId}/audit`);
}
