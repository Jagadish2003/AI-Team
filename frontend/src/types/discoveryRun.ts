export type RunStatus = "RUNNING" | "COMPLETED" | "FAILED";
export type StepStatus = "PENDING" | "RUNNING" | "DONE" | "FAILED";

export interface RunStep {
  id: string;
  label: string;
  status: StepStatus;
}

export interface RunInputs {
  connectedSources: string[];
  uploadedFiles: string[];
  sampleWorkspaceEnabled: boolean;
  totalSources?: number;
  mode?: "offline" | "live";
}

export interface RunProgress {
  percent: number;
  currentStepId: string;
  etaSeconds: number;
}

export interface RunSummary {
  appsDetected: number;
  workflowsInferred: number;
  opportunitiesFound: number;
  confidence: "LOW" | "MEDIUM" | "HIGH";
  warnings: number;
}

export interface DiscoveryRun {
  id?: string; // backend contract field
  runId?: string; // legacy UI field
  status: RunStatus | "running" | "complete" | "failed";
  startedAt: string;
  updatedAt: string;
  inputs: RunInputs;
  progress: RunProgress;
  steps: RunStep[];
  summary: RunSummary;
  packId?: string;
  packIds?: string[];
  packVersions?: Record<string, string>;
  focusId?: string | null;
  industryId?: string | null;
  templateId?: string | null;
  templateIds?: string[];
  templateVersions?: Record<string, string>;
  selectedSystemIds?: string[];
  templateProvenance?: {
    template_id?: string | null;
    template_ids?: string[];
    applied: boolean;
    untouched: boolean;
    edited_fields: string[];
    template_defaults?: Record<string, unknown> | null;
    template_defaults_list?: Array<Record<string, unknown>>;
    pack_boundaries?: Array<Record<string, unknown>>;
  };
}

export type LogLevel = "INFO" | "WARNING" | "ERROR";
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

export interface StartRunResponse {
  runId: string;
  status: string;
  startedAt: string;
}
