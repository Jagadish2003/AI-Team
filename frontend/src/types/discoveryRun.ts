export type RunStatus = 'RUNNING' | 'COMPLETED' | 'FAILED';
export type StepStatus = 'PENDING' | 'RUNNING' | 'DONE' | 'FAILED';

export interface RunStep { id: string; label: string; status: StepStatus; }

export interface RunInputs {
  connectedSources: string[];
  uploadedFiles: string[];
  sampleWorkspaceEnabled: boolean;
  totalSources?: number;
}

export interface RunProgress { percent: number; currentStepId: string; etaSeconds: number; }

export interface RunSummary {
  appsDetected: number;
  workflowsInferred: number;
  opportunitiesFound: number;
  confidence: 'LOW' | 'MEDIUM' | 'HIGH';
  warnings: number;
}

export interface DiscoveryRun {
  id?: string;        // backend contract field
  runId?: string;     // legacy UI field
  status: RunStatus | "running" | "complete" | "failed";
  startedAt: string;
  updatedAt: string;
  inputs: RunInputs;
  progress: RunProgress;
  steps: RunStep[];
  summary: RunSummary;
}

export type LogLevel = 'INFO' | 'WARNING' | 'ERROR';
export interface RunEvent {
  // backend contract fields
  id?: string;
  tsLabel?: string;
  stage?: string;
  // UI / log fields
  ts?: string;
  level?: LogLevel;
  message: string;
}
