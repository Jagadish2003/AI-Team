"""
retrieval_trace.py — Release 2.0-B1 T2: assembly decision transparency.

"Retrieval proposes, assembly decides" (context_assembly.py) is already
enforced structurally between retrieval.evidence_source and
context_assembly.assemble_context(): the evidence source never excludes
anything itself, and assemble_context()'s selection_log records exactly one
entry per candidate it considered, decision "included" or "excluded" with a
reason. What is missing is a per-OPPORTUNITY record of that decision that a
later trace can read back:

  - assemble_context() is invoked exactly once per RUN today
    (graph_context.build_graph_context, called from llm_enrichment.py before
    its opportunity loop), against a synthetic {"run_id": run_id} object with
    no query text — so the retrieval evidence source proposes nothing in
    production, and even when it does, the resulting selection_log is a local
    variable discarded once enrichment finishes. Never persisted anywhere.

This module closes that gap:

  - assemble_evidence_candidates_for_opportunity(...) runs assembly for ONE
    real opportunity (query text derived from its title/rationale) and
    returns every retrieval candidate it considered, used and unused alike.
    The selection_log itself only carries candidate_id/origin/confidence/
    decision/reason — not enough to identify a candidate on its own — so this
    wraps the evidence_source callable to also capture each candidate's
    source_system/source_artifact/content, merging both into one record.
  - store_retrieval_candidates(...) / get_retrieval_candidates_for_opportunity(...)
    persist/read that record per (run_id, opp_id), the same run-scoped KV
    pattern app.evidence_pointers uses for the source-record spine.
  - record_retrieval_candidates_for_opportunity(...) does both in one call —
    the hook llm_enrichment.py's per-opportunity loop calls.

Both used AND unused candidates are recorded — retrieval proposes, assembly
decides, and both sides of that decision are visible (2.0-B1 AC3). Every
function here is advisory: a failure degrades to an empty/no-op result,
never raises, and never blocks enrichment or the run.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import db

logger = logging.getLogger(__name__)

# Run-scoped KV namespace for the per-opportunity retrieval-candidate index
# (parallel to evidence_pointers.py's "evidence_pointers" bucket).
KV_RETRIEVAL_CANDIDATES = "retrieval_candidates"

DECISION_INCLUDED = "included"
DECISION_EXCLUDED = "excluded"

_MAX_CONTENT_SNIPPET_CHARS = 280
_MAX_QUERY_CHARS = 2000


def _query_text_for_opportunity(opportunity: Mapping[str, Any]) -> Optional[str]:
    """Derive a retrieval query from a stored (Track-A shaped) opportunity.

    Combines title + rationale/description for a more descriptive query than
    title alone; falls back to whichever is present. None when neither
    exists — the caller then proposes nothing (there is nothing to search
    for), matching the standing "no query text => no candidates" behaviour of
    retrieval.evidence_source.opportunity_query_text.
    """
    title = str(opportunity.get("title") or "").strip()
    rationale = str(
        opportunity.get("aiRationale") or opportunity.get("description") or ""
    ).strip()
    parts = [p for p in (title, rationale) if p]
    if not parts:
        return None
    return ". ".join(parts)[:_MAX_QUERY_CHARS]


def _snippet(content: Any) -> Optional[str]:
    text = str(content or "").strip()
    if not text:
        return None
    if len(text) <= _MAX_CONTENT_SNIPPET_CHARS:
        return text
    return text[:_MAX_CONTENT_SNIPPET_CHARS].rstrip() + "..."


def _default_evidence_source_factory(org_id: str) -> Callable[..., Any]:
    from .retrieval.evidence_source import retrieval_evidence_source

    return retrieval_evidence_source(org_id)


def assemble_evidence_candidates_for_opportunity(
    org_id: Optional[str],
    opportunity: Mapping[str, Any],
    *,
    evidence_source_factory: Optional[Callable[[str], Callable[..., Any]]] = None,
) -> List[Dict[str, Any]]:
    """Run context assembly for ONE opportunity; return every retrieval
    candidate it considered — used and unused alike.

    Each record:
      {
        "chunk_id": str,
        "used": bool,                 # decision == "included"
        "decision": "included" | "excluded",
        "reason": str,                 # e.g. "included@position_1" / "ranked_out"
        "confidence": float,
        "origin": "observed" | "inferred",
        "source_system": Optional[str],
        "source_artifact": Optional[str],
        "content_snippet": Optional[str],
        "is_stale": Optional[bool],
      }

    Never raises: no org, no usable query text, an unavailable retrieval
    store, or an assembly error all degrade to an empty list.
    ``evidence_source_factory`` is injectable for tests (defaults to the real
    retrieval-backed source).
    """
    if not org_id:
        return []
    query_text = _query_text_for_opportunity(opportunity)
    if not query_text:
        return []

    try:
        from .context_assembly import AssemblyPolicy, assemble_context

        factory = evidence_source_factory or _default_evidence_source_factory
        base_source = factory(org_id)

        # The selection_log alone doesn't carry enough to identify a
        # candidate (no source_system/source_artifact/content) — capture the
        # full proposed set ourselves so both used AND unused entries can be
        # enriched with it below. assemble_context() never exposes an
        # excluded candidate's payload on its own (only included ones survive
        # into package.evidence), so this wrapper is the only way to recover it.
        raw_candidates: List[Dict[str, Any]] = []

        def _capturing_source(opp_arg: Any, policy: Any = None) -> List[Any]:
            candidates = base_source(opp_arg, policy) or []
            raw_candidates.extend(c for c in candidates if isinstance(c, dict))
            return candidates

        assembly_opportunity = {"id": opportunity.get("id"), "query_text": query_text}
        package = assemble_context(
            opportunity=assembly_opportunity,
            graph={"entities": [], "relationships": []},
            policy=AssemblyPolicy(),
            evidence_source=_capturing_source,
        )
    except Exception as exc:  # noqa: BLE001 — assembly-trace capture is advisory.
        logger.debug(
            "retrieval_trace: assembly failed for opp %s: %s",
            opportunity.get("id"), exc,
        )
        return []

    candidates_by_id = {
        str(c.get("chunk_id")): c for c in raw_candidates if c.get("chunk_id")
    }

    records: List[Dict[str, Any]] = []
    for entry in package.selection_log:
        if entry.get("kind") != "evidence":
            continue
        candidate_id = str(entry.get("candidate_id") or "")
        raw = candidates_by_id.get(candidate_id, {})
        decision = entry.get("decision")
        records.append({
            "chunk_id": candidate_id,
            "used": decision == DECISION_INCLUDED,
            "decision": decision,
            "reason": entry.get("reason"),
            "confidence": entry.get("confidence"),
            "origin": entry.get("origin"),
            "source_system": raw.get("source_system"),
            "source_artifact": raw.get("source_artifact"),
            "content_snippet": _snippet(raw.get("content")),
            "is_stale": raw.get("is_stale"),
        })
    return records


def store_retrieval_candidates(
    run_id: str, opp_id: str, candidates: Sequence[Mapping[str, Any]]
) -> None:
    """Persist one opportunity's retrieval-candidate record (used + unused).

    Never raises — additive, non-blocking, mirrors
    evidence_pointers.store_evidence_pointers' failure posture: a storage
    failure is logged and swallowed, never breaks the run.
    """
    if not run_id or not opp_id:
        return
    try:
        index = db.run_kv_get(KV_RETRIEVAL_CANDIDATES, run_id, {}) or {}
        if not isinstance(index, dict):
            index = {}
        index[str(opp_id)] = [dict(c) for c in candidates]
        db.run_kv_set(KV_RETRIEVAL_CANDIDATES, run_id, index)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "retrieval_trace: storage failed (non-blocking) run=%s opp=%s: %s",
            run_id, opp_id, exc,
        )


def get_retrieval_candidates_for_opportunity(run_id: str, opp_id: str) -> List[Dict[str, Any]]:
    """Read back the stored retrieval-candidate record for one opportunity.

    Returns [] when nothing was stored (an older run, a run whose assembly
    step proposed nothing, or a storage failure) — never raises.
    """
    try:
        index = db.run_kv_get(KV_RETRIEVAL_CANDIDATES, run_id, {}) or {}
    except Exception:
        return []
    if not isinstance(index, dict):
        return []
    candidates = index.get(str(opp_id), [])
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates if isinstance(c, dict)]


def record_retrieval_candidates_for_opportunity(
    org_id: Optional[str],
    run_id: str,
    opportunity: Mapping[str, Any],
    *,
    evidence_source_factory: Optional[Callable[[str], Callable[..., Any]]] = None,
) -> List[Dict[str, Any]]:
    """Assemble + persist in one call — the hook llm_enrichment.py's
    per-opportunity loop calls. Never raises."""
    try:
        opp_id = str(opportunity.get("id") or "")
        if not opp_id:
            return []
        candidates = assemble_evidence_candidates_for_opportunity(
            org_id, opportunity, evidence_source_factory=evidence_source_factory
        )
        if candidates:
            store_retrieval_candidates(run_id, opp_id, candidates)
        return candidates
    except Exception as exc:  # noqa: BLE001 — never blocks enrichment.
        logger.debug("retrieval_trace: record failed (non-blocking): %s", exc)
        return []


__all__ = [
    "KV_RETRIEVAL_CANDIDATES",
    "DECISION_INCLUDED",
    "DECISION_EXCLUDED",
    "assemble_evidence_candidates_for_opportunity",
    "store_retrieval_candidates",
    "get_retrieval_candidates_for_opportunity",
    "record_retrieval_candidates_for_opportunity",
]
