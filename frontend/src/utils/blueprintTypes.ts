/**
 * T41-1 — Blueprint shared types
 *
 * These types match the BlueprintResponse shape returned by the backend
 * GET /api/runs/{runId}/opportunities/{oppId}/blueprint endpoint.
 *
 * The frontend renders from this shape. No derivation logic lives here.
 * Single source of truth: backend routes_sprint41_blueprint.py
 */

export interface BlueprintAction {
  action: string;
  object: string;
  detail: string;
}

export interface BlueprintComplexity {
  label: string;
  description: string;
  tier: string;
}

export interface BlueprintResponse {
  oppId: string;
  agentName: string;
  agentTopic: string;
  agentTopicIsLlm: boolean;
  suggestedActions: BlueprintAction[];
  guardrails: string[];
  agentforcePermissions: string[];
  complexity: BlueprintComplexity;
  evidenceIds: string[];
  detectorId: string;
}

/**
 * Extended OpportunityCandidate shape that includes _debug fields
 * persisted by track_a_adapter.py.
 *
 * _debug is not in the original TS contract (analystReview.ts) because
 * it was marked as a debug namespace. We extend here to surface
 * detector_id for blueprint routing without modifying the base type.
 */
export interface OpportunityCandidateWithDebug {
  id: string;
  title: string;
  category: string;
  tier: string;
  decision: string;
  impact: number;
  effort: number;
  confidence: string;
  aiRationale: string;
  evidenceIds: string[];
  requiredPermissions?: string[];
  _debug?: {
    detector_id: string;
    signal_source: string;
    metric_value: number;
    threshold: number;
    roadmap_stage: string;
  };
}
