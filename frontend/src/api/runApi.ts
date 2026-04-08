import { apiGet, apiPost } from "../lib/apiClient";
import type { DiscoveryRun, RunEvent, RunInputs } from "../types/discoveryRun";

export interface StartRunResponse {
  runId: string;
  status: string;
  startedAt: string;
}

export function startRun(inputs: RunInputs): Promise<StartRunResponse> {
  return apiPost<StartRunResponse>(`/api/runs/start`, inputs);
}

export function fetchRun(runId: string): Promise<DiscoveryRun> {
  return apiGet<DiscoveryRun>(`/api/runs/${runId}`);
}

export function fetchRunEvents(runId: string): Promise<RunEvent[]> {
  return apiGet<RunEvent[]>(`/api/runs/${runId}/events`);
}
