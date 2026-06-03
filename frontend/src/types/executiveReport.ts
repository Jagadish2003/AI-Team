import type { OpportunityCandidate } from './analystReview';

export interface SourcesAnalyzed {
  recommendedConnected: number;
  totalConnected: number;
  uploadedFiles: number;
  sampleWorkspaceEnabled?: boolean;
}

export interface SnapshotBubble {
  x: number;
  y: number;
  r: number;
}

export interface RoadmapHighlights {
  next30Count: number;
  next60Count: number;
  next90Count: number;
  blockerCount: number;
}

/** Title-case confidence level returned by GET /api/runs/{runId}/executive-report */
export type ConfidenceLevel = 'High' | 'Moderate' | 'Low';

export interface ExecutiveReport {
  confidence: ConfidenceLevel;
  sourcesAnalyzed: SourcesAnalyzed;
  topQuickWins: OpportunityCandidate[];
  snapshotBubbles: SnapshotBubble[];
  roadmapHighlights: RoadmapHighlights;
}
