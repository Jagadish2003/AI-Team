import type { OpportunityCandidate } from './analystReview';
import type { PackCertification } from './packCertification';

export type ExecutiveReportConfidence = 'High' | 'Moderate' | 'Low';

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
  // 2.0-C2 T3 (AT-833 / AC2): which LEVEL of pack produced the claims in this
  // report, in order of first appearance. Frozen into the artifact at generation
  // time — an export states what was verifiable when it was produced.
  packCertifications?: PackCertification[];
}
