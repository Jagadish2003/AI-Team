/**
 * 2.0-B2 T3 — Entity Match review API wrapper.
 *
 * Typed client over the analyst+ endpoints behind the cross-source entity match
 * review surface:
 *   GET  /api/entity-match-proposals               → the org's review queue + counts
 *   GET  /api/entity-match-proposals/{id}          → one proposal + decision history
 *   POST /api/entity-match-proposals/{id}/decision → confirm / reject
 *   POST /api/entity-match-proposals/scan          → recompute proposals
 *
 * Only PROPOSED matches reach these endpoints. The resolution engine auto-merges
 * where identity is stated (an explicit cross-reference, or the org alias table);
 * a name-similarity match is never merged and is queued here for a person. So a
 * decision made through this client records an answer — it does not itself merge
 * the graph.
 *
 * Goes through the shared apiClient so requests carry the in-session JWT and the
 * org is resolved server-side. Callers handle ApiError.
 */
import { apiGet, apiPost } from "../lib/apiClient";

export type ProposalStatus = "pending" | "confirmed" | "rejected";
/** `undo` withdraws a confirm/reject and returns the pair to `pending`. */
export type ProposalAction = "confirm" | "reject" | "undo";

/** One side of a proposed pair, as the reviewer sees it. */
export interface ProposalEntityView {
  entity_id: string | null;
  display_name: string | null;
  canonical_name: string | null;
  entity_type: string | null;
  source_system: string | null;
  source_record_id: string | null;
}

/** A shared observed relationship that corroborated the proposal. */
export interface CorroboratingRelationship {
  relationship_type: string;
  entity_id: string;
}

/**
 * The evidence snapshot stored WITH the proposal, so the surface can explain why
 * a pair was proposed without re-running the engine — and so a decision stays
 * explainable against the evidence that existed when it was made.
 */
export interface ProposalEvidence {
  subject?: ProposalEntityView;
  target?: ProposalEntityView;
  tier?: string | null;
  confidence?: number | null;
  reason?: string;
  corroborating_relationships?: CorroboratingRelationship[];
  canonical_name?: string | null;
  subject_source?: string | null;
  target_source?: string | null;
}

export interface EntityMatchProposal {
  org_id: string;
  proposal_id: string;
  entity_type: string;
  left_entity_id: string;
  right_entity_id: string;
  tier: string;
  confidence: number;
  status: ProposalStatus;
  evidence: ProposalEvidence;
  revision: number;
  decided_by: string | null;
  decided_at: string | null;
  note: string | null;
  first_proposed_at: string | null;
  last_proposed_at: string | null;
}

export interface ProposalHistoryEntry {
  id: string;
  proposal_id: string;
  revision: number;
  action: ProposalAction;
  previous_status: ProposalStatus;
  resulting_status: ProposalStatus;
  actor_id: string;
  note: string | null;
  decided_at: string | null;
}

export interface ProposalListResponse {
  proposals: EntityMatchProposal[];
  /** Always carries all three statuses (zero-filled) — the tabs never have to
   *  tell "none" apart from "not reported". */
  counts: Record<ProposalStatus, number>;
  status: ProposalStatus | null;
}

export interface ProposalDetailResponse {
  proposal: EntityMatchProposal;
  history: ProposalHistoryEntry[];
}

export interface ProposalDecisionResponse {
  proposal: EntityMatchProposal;
  action: ProposalAction;
  previous_status: ProposalStatus;
  resulting_status: ProposalStatus;
  revision: number;
  /** false when the decision already in force was repeated (a double-click) — no
   *  duplicate history row was written. */
  changed: boolean;
  actor_id: string;
  decided_at: string;
}

export interface ProposalScanResponse {
  created: number;
  refreshed: number;
  /** Pairs the engine proposed again that a human has already answered. Reported
   *  rather than hidden: the queue did not grow, and this says how often that
   *  rule fired. */
  skipped_already_decided: number;
  entity_types: string[];
}

/** GET the org's review queue. Omit `status` for every status. */
export async function fetchEntityMatchProposals(
  status?: ProposalStatus,
): Promise<ProposalListResponse> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiGet<ProposalListResponse>(`/api/entity-match-proposals${query}`);
}

/** GET one proposal with its evidence and full decision history. */
export async function fetchEntityMatchProposal(
  proposalId: string,
): Promise<ProposalDetailResponse> {
  return apiGet<ProposalDetailResponse>(
    `/api/entity-match-proposals/${encodeURIComponent(proposalId)}`,
  );
}

/**
 * Confirm or reject one proposed match. Idempotent server-side: repeating the
 * decision already in force resolves with `changed: false` and writes no
 * duplicate history row.
 */
export async function decideEntityMatchProposal(
  proposalId: string,
  action: ProposalAction,
  note?: string,
): Promise<ProposalDecisionResponse> {
  return apiPost<ProposalDecisionResponse>(
    `/api/entity-match-proposals/${encodeURIComponent(proposalId)}/decision`,
    { action, note: note ?? null },
  );
}

/**
 * Recompute this org's proposals from the ranked engine. Writes nothing to the
 * graph and never re-opens an already-answered pair.
 */
export async function scanEntityMatchProposals(): Promise<ProposalScanResponse> {
  return apiPost<ProposalScanResponse>("/api/entity-match-proposals/scan", {});
}
