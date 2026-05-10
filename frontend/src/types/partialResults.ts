export type Confidence = 'LOW' | 'MEDIUM' | 'HIGH';
export type EntityType = 'Application' | 'Workflow' | 'Service' | 'Role' | 'DataObject' | 'Other';

export interface ExtractedEntity {
  id: string;
  name: string;
  type: EntityType;
  mentionCount: number;
  confidence: Confidence;
}

export type EvidenceType = 'Email' | 'Ticket' | 'Chat' | 'Doc' | 'Log' | 'Metric';
export type ReviewDecision = 'UNREVIEWED' | 'APPROVED' | 'REJECTED';

export interface EvidenceReview {
  id: string;
  tsLabel: string;
  source: string;
  evidenceType: EvidenceType;
  title: string;
  snippet: string;
  entities: string[];      // entity ids
  confidence: Confidence;
  decision: ReviewDecision;
}
