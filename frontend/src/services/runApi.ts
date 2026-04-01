import { apiPost, apiGet } from "../lib/apiClient";
import type { DiscoveryRun, RunEvent } from "../types/discoveryRun";

export type RunStartInputs = {
  connectedSources: string[];
  uploadedFiles: string[];
  sampleWorkspaceEnabled: boolean;
};

export type RunStartResponse = {
  runId: string;
  status: string;
  startedAt: string;
};

export function startRun(inputs: RunStartInputs) {
  return apiPost<RunStartResponse>("/api/runs/start", inputs);
}

export function fetchRun(runId: string): Promise<DiscoveryRun> {
  return apiGet<DiscoveryRun>(`/api/runs/${runId}`);
}
export function fetchRunEvents(runId: string): Promise<RunEvent[]> {
  return apiGet<RunEvent[]>(`/api/runs/${runId}/events`);
}
