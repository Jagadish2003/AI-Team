import type { OpportunityCandidate } from './analystReview';

export type ExecutiveReportConfidence =
  | 'HIGH'
  | 'MODERATE'
  | 'LOW'
  | 'High'
  | 'Moderate'
  | 'Low';

export interface SourcesAnalyzed {
  recommendedConnected: number;
  totalConnected: number;
  uploadedFiles: number;
  sampleWorkspaceEnabled?: boolean;
}

export interface ExecutiveSnapshotBubble {
  id?: string;
  label?: string;
  value?: string | number;
  source?: string;
  [key: string]: unknown;
}

export interface ExecutiveRoadmapHighlights {
  next30Count?: number;
  next60Count?: number;
  next90Count?: number;
  blockerCount?: number;
  [key: string]: unknown;
}

export interface ExecutiveReport {
  confidence: ExecutiveReportConfidence;
  sourcesAnalyzed: SourcesAnalyzed;
  topQuickWins: OpportunityCandidate[];
  snapshotBubbles: ExecutiveSnapshotBubble[];
  roadmapHighlights: ExecutiveRoadmapHighlights;
  aiExecutiveSummary?: string;
}
