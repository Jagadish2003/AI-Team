import { apiGet } from '../lib/apiClient';
import type { PilotRoadmapModel } from '../types/pilotRoadmap';

// Backend returns different field names — map to the frontend type.
function mapRoadmap(raw: any): PilotRoadmapModel {
  return {
    selectedOpportunityCount: raw.selectedCount ?? 0,
    requiredPermissionsCount: raw.permissionsRequiredCount ?? 0,
    dependencyCount: raw.dependenciesCount ?? 0,
    overallReadiness: raw.overallReadiness ?? 'Low',
    stages: raw.stages ?? [],
  };
}

export async function fetchRunRoadmap(runId: string): Promise<PilotRoadmapModel> {
  const raw = await apiGet<any>(`/api/runs/${runId}/roadmap`);
  return mapRoadmap(raw);
}

export interface SourcesAnalyzed {
  recommendedConnected: number;
  totalConnected: number;
  uploadedFiles: number;
  sampleWorkspaceEnabled?: boolean;
}

export interface ExecutiveReport {
  confidence: string;
  sourcesAnalyzed: SourcesAnalyzed;
  topQuickWins: any[];
  snapshotBubbles: any[];
  roadmapHighlights: any;
}

export async function fetchRunExecutiveReport(runId: string): Promise<ExecutiveReport> {
  return apiGet<ExecutiveReport>(`/api/runs/${runId}/executive-report`);
}
