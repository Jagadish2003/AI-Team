import { apiGet, apiPost } from "../lib/apiClient";
import type { DiscoveryRun, RunEvent, RunInputs, StartRunResponse } from "../types/discoveryRun";
import type { EvidenceReview, ExtractedEntity } from "../types/partialResults";

export function startRun(inputs: RunInputs): Promise<StartRunResponse> {
  return apiPost<StartRunResponse>(`/api/runs/start`, inputs);
}

export function fetchRun(runId: string): Promise<DiscoveryRun> {
  return apiGet<DiscoveryRun>(`/api/runs/${runId}`);
}

export function fetchRunEvents(runId: string): Promise<RunEvent[]> {
  return apiGet<RunEvent[]>(`/api/runs/${runId}/events`);
}

export function replayRun(runId: string): Promise<{ ok: boolean; runId?: string }> {
  return apiPost<{ ok: boolean; runId?: string }>(`/api/runs/${runId}/replay`, {});
}

export function fetchEvidence(runId: string): Promise<EvidenceReview[]> {
  return apiGet<EvidenceReview[]>(`/api/runs/${runId}/evidence`);
}

export function fetchEntities(runId: string): Promise<ExtractedEntity[]> {
  return apiGet<ExtractedEntity[]>(`/api/runs/${runId}/entities`);
}
