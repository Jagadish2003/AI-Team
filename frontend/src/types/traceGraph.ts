// 2.0-B1 T3 — Interrogation UI (drill-down) types.
// Shapes mirror the backend response exactly (snake_case), like the temporal
// fields in enrichment.ts — this is the same intentional exception to the
// camelCase frontend convention: it's a direct pass-through of
// GET /api/runs/{runId}/opportunities/{oppId}/trace-graph.

export type HopOrigin = 'observed' | 'inferred';
export type HopType = 'finding' | 'evidence' | 'source_record';

// A hop's `detail` is a free-form record: source-record hops opportunistically
// carry a `source_url` (or `deep_link`) string when the underlying connector
// supplies one (e.g. ServiceNow-sourced artifacts) — most connectors do not
// populate it today, so the UI must treat it as optional and never fabricate
// a link when absent ("deep link where the connector supports it").
export interface TraceHop {
  hop_id: string;
  hop_type: HopType;
  label: string;
  origin: HopOrigin;
  connector: string | null;
  run_id: string;
  timestamp: string | null;
  from_hop_id: string | null;
  detail: Record<string, unknown>;
}

// One MSP-B7 correlation-window join backing a corroborated claim.
// `hop_id` names the hop this join corroborates (usually the event_signature
// source-record hop) — null when the join could not be attributed to a hop.
export interface JoinTrace {
  join_type: string;
  window_seconds: number | null;
  delta_seconds: number | null;
  within_window: boolean;
  a_at: string | null;
  b_at: string | null;
  hop_id: string | null;
}

// One retrieval candidate context assembly considered — used or not
// ("retrieval proposes, assembly decides", 2.0-B1 AC3).
export interface RetrievalCandidate {
  chunk_id: string;
  used: boolean;
  decision: string;
  reason: string | null;
  confidence: number | null;
  origin: string | null;
  source_system: string | null;
  source_artifact: string | null;
  content_snippet: string | null;
  is_stale: boolean | null;
}

// GET /api/runs/{runId}/opportunities/{oppId}/trace-graph
export interface TraceGraphResponse {
  runId: string;
  oppId: string;
  hops: TraceHop[];
  joins: JoinTrace[];
  complete: boolean;
  truncated: boolean;
  retrieval_candidates: RetrievalCandidate[];
  retrieval_candidates_used_count: number;
  retrieval_candidates_unused_count: number;
  // False (never absent) when the run belongs to another org or the
  // opportunity has no derivable chain yet — mirrors the evidence-trace and
  // enrichment endpoints' "available" contract.
  available: boolean;
}

/** Best-effort deep-link extraction from a hop's free-form `detail` — the
 * only two key names the backend has been observed to opportunistically
 * populate. Returns null (never a fabricated link) when neither is present. */
export function hopDeepLink(hop: TraceHop): string | null {
  const value = hop.detail?.source_url ?? hop.detail?.deep_link;
  return typeof value === 'string' && value.trim().length > 0 ? value : null;
}
