import type { Decision, Confidence } from "./common";

export interface EvidenceReview {
  id: string;
  tsLabel: string;
  source: string;
  evidenceType: string;
  title: string;
  snippet: string;
  entities: string[];
  confidence: Confidence;
  decision: Decision;
}
