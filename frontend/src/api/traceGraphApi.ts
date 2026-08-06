import { apiGet } from '../lib/apiClient';
import type { TraceGraphResponse } from '../types/traceGraph';

export type {
  TraceGraphResponse,
  TraceHop,
  JoinTrace,
  RetrievalCandidate,
  HopOrigin,
  HopType,
} from '../types/traceGraph';
export { hopDeepLink } from '../types/traceGraph';

// 2.0-B1 T1/T2/T3 — the finding's full provenance chain (finding -> evidence
// -> source records), MSP-B7 join/correlation-window surfacing, and
// used-vs-unused retrieval candidates. Never 404s for a merely thin/empty
// chain — check `available` on the response.
export async function fetchTraceGraph(
  runId: string,
  oppId: string
): Promise<TraceGraphResponse> {
  return apiGet<TraceGraphResponse>(
    `/api/runs/${runId}/opportunities/${oppId}/trace-graph`
  );
}
