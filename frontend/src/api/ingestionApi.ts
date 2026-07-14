import { apiPost } from "../lib/apiClient";

/**
 * Response of the owner-only checkpoint-reset operation
 * (POST /api/ingestion/checkpoints/reset). `cleared` reports whether a
 * checkpoint actually existed and was cleared: true = a checkpoint existed and
 * was removed (the next ingestion re-reads from the start); false = there was
 * nothing to clear (the source was already at first-run).
 */
export interface CheckpointResetResponse {
  ok: boolean;
  org_id: string;
  connector_id: string;
  cleared: boolean;
}

/**
 * Reset a connector's ingestion checkpoint. This is the authoritative,
 * owner-only reset path already exposed by the backend — the Run-Health
 * dashboard surfaces it in context, it does not introduce a new control. The
 * org is resolved server-side from the caller's session, never sent here.
 */
export const resetIngestionCheckpoint = (
  connectorId: string,
): Promise<CheckpointResetResponse> =>
  apiPost("/api/ingestion/checkpoints/reset", { connector_id: connectorId });
