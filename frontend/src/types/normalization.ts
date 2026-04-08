export type MappingStatus = 'MAPPED' | 'UNMAPPED' | 'AMBIGUOUS';
export type Confidence = 'LOW' | 'MEDIUM' | 'HIGH';

export interface MappingRow {
  id: string;
  sourceSystem: string;
  sourceType: string;
  sourceField: string;
  commonEntity: string;
  commonField: string;
  status: MappingStatus;
  confidence: Confidence;
  sampleValues: string[];
  notes?: string;
}

export interface PermissionRequirement {
  id: string;
  label: string;
  sourceSystem: string;
  required: boolean;
  satisfied: boolean;
}

export interface ConfidenceExplanation {
  level: Confidence;
  why: string;
  missingSignals: string[];
  nextAction: string;
}
