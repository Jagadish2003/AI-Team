/**
 * T41-1 — Blueprint API client
 *
 * Single source of truth: backend blueprint endpoint.
 * Frontend calls fetchBlueprint() and renders the response.
 * No local derivation. No category mapping. No parallel logic.
 *
 * If the backend is unreachable, the page renders a loading error state.
 * There is no silent fallback to a local derivation path.
 */
import { apiGet } from '../lib/apiClient';
import type { BlueprintResponse } from '../utils/blueprintTypes';

export type { BlueprintResponse };

/**
 * Fetch the Agentforce Blueprint for a specific opportunity.
 * Computed deterministically on the backend from existing run data.
 * Same runId + oppId always returns the same blueprint.
 *
 * @throws ApiError with status 404 if runId or oppId is unknown.
 */
export async function fetchBlueprint(
  runId: string,
  oppId: string,
): Promise<BlueprintResponse> {
  return apiGet<BlueprintResponse>(
    `/api/runs/${runId}/opportunities/${oppId}/blueprint`,
  );
}
