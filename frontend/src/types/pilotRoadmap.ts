import { OpportunityCandidate, PermissionItem } from './analystReview';

export type Readiness = 'READY' | 'PENDING' | 'MISSING';

export interface RoadmapDependency {
  id: string;
  label: string;
  status: Readiness;
}

export interface RoadmapStage {
  id: 'NEXT_30' | 'NEXT_60' | 'NEXT_90';
  title: string;
  summary: string;
  opportunities: OpportunityCandidate[];
  requiredPermissions: PermissionItem[];
  dependencies: RoadmapDependency[];
}

export interface PilotRoadmapModel {
  selectedOpportunityCount: number;
  requiredPermissionsCount: number;
  dependencyCount: number;
  overallReadiness: 'High' | 'Moderate' | 'Low';
  stages: RoadmapStage[];
}
