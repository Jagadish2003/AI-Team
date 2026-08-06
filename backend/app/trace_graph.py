"""
trace_graph.py — Release 2.0-B1 T1: the trace graph engine.

Builds the complete provenance chain for one finding (opportunity): finding ->
contributing evidence -> source records -> connector/run provenance. Every
hop in the chain carries its origin ('observed'/'inferred'), the connector
(source system) that produced it, the run id, and a timestamp — the minimum
a third party needs to audit where a claim came from (2.0-B1 AC1).

Where a claim was corroborated by a time-windowed join (MSP-B7's event<->
incident / event<->event correlation, see discovery/correlation/windows.py),
the trace also surfaces the join type and the correlation window actually
used. A join outside its configured window can never reach this trace:
MSP-B7's runtime only folds within-window joins into a finding's corroborating
incident set in the first place, and
``discovery.packs.cloud_ops_finding.build_corroboration`` defensively
re-filters to ``within_window=True`` entries before a finding's corroboration
is ever built. This module adds one more independent layer of the same
guarantee — ``_within_window_joins`` drops any out-of-window entry a caller
might pass, so the property holds even if a future caller hands this module
the raw, unfiltered MSP-B7 trace list (2.0-B1 AC2).

Two entry points:

  build_finding_trace(...)  — pure, DB-free. Given an opportunity dict, its
                               evidence items, and (optionally) its evidence
                               pointers, returns a FindingTrace. Never raises.

  load_finding_trace(...)   — DB-aware convenience wrapper used by the route
                               layer (routes_trace_graph.py). Resolves the
                               opportunity/evidence/pointers for one run from
                               run-scoped storage and calls build_finding_trace.
                               Does NOT enforce tenancy itself (see
                               evidence_pointers.py's identical contract) — the
                               route layer owns the org-boundary check.

This module is read-only and side-effect free.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import db

logger = logging.getLogger(__name__)

# ── Hop / origin vocabulary ─────────────────────────────────────────────────

HOP_FINDING = "finding"
HOP_EVIDENCE = "evidence"
HOP_SOURCE_RECORD = "source_record"

ORIGIN_OBSERVED = "observed"
ORIGIN_INFERRED = "inferred"

# Why a chain stops short of AC1's "terminating in source records". Enumerated
# rather than free text so a surface can branch on it, and so the difference
# between "this finding has nothing" and "this finding's provenance was never
# recorded" is not lost in prose — they need different remedies.
#: No hops at all: the opportunity carries no evidence and no source trace.
REASON_NO_TRACE = "no_chain"
#: Evidence is attached, but nothing resolves to an originating record — no
#: evidence pointers were stored for the run and the finding contract carries no
#: source_trace artifacts. The usual cause is a run materialized before pointer
#: storage was in place; re-running the discovery pipeline populates it.
REASON_NO_SOURCE_RECORD = "no_source_record"
_VALID_ORIGINS = (ORIGIN_OBSERVED, ORIGIN_INFERRED)

# A generous ceiling on hops per trace — matches graph_query.py's node-cap
# philosophy (bounded traversal, never unbounded). A finding legitimately
# producing this many hops is not expected; this is a backstop, not a tuned
# limit.
_MAX_HOPS = 500

# Evidence 'source' label -> connector-style system id. Mirrors
# app.evidence_pointers._SOURCE_SYSTEM_BY_LABEL so the evidence-pointer spine
# and the trace-graph spine never disagree on a connector's canonical id.
_SOURCE_SYSTEM_BY_LABEL: Dict[str, str] = {
    "salesforce": "salesforce",
    "servicenow": "servicenow",
    "jira": "jira",
    "github": "github",
}

# Best-known connector for a cloud_ops/security_ops finding_contract
# source_trace artifact type. These packs build their contract straight from
# ITSM/event data (cloud_ops_finding.py), never through evidence_builder.py,
# so an individual artifact carries no explicit connector of its own — the
# artifact type is the closest honest signal available.
_ARTIFACT_TYPE_CONNECTOR: Dict[str, str] = {
    "incident": "servicenow",
    "event_signature": "events",
    "shared_ci": "graph",
    "service": "servicenow",
    "recurrence_signature": "servicenow",
}


def _connector_for_source(label: Any, fallback: Any = None) -> Optional[str]:
    for candidate in (label, fallback):
        if candidate and str(candidate).strip():
            key = str(candidate).strip().lower()
            return _SOURCE_SYSTEM_BY_LABEL.get(key, key)
    return None


def _origin_of(value: Any, default: str = ORIGIN_OBSERVED) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_ORIGINS else default


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TraceHop:
    """One node in a finding's provenance chain.

    ``from_hop_id`` is None only for the root (the finding itself); every
    other hop names the hop it was reached from, so the chain is a tree
    rooted at the finding (2.0-B1 AC1: "every hop carries origin, connector,
    run id, and timestamp").
    """

    hop_id: str
    hop_type: str
    label: str
    origin: str
    connector: Optional[str]
    run_id: str
    timestamp: Optional[str]
    from_hop_id: Optional[str]
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hop_id": self.hop_id,
            "hop_type": self.hop_type,
            "label": self.label,
            "origin": self.origin,
            "connector": self.connector,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "from_hop_id": self.from_hop_id,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class JoinTrace:
    """One MSP-B7 correlation-window join backing a corroborated claim.

    Mirrors ``discovery.correlation.windows.WindowJoin.to_trace()`` field for
    field so the two never drift. Always ``within_window=True`` — see the
    module docstring; an out-of-window entry cannot reach this dataclass.
    """

    join_type: str
    window_seconds: Optional[int]
    delta_seconds: Optional[float]
    within_window: bool
    a_at: Optional[str]
    b_at: Optional[str]
    hop_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "join_type": self.join_type,
            "window_seconds": self.window_seconds,
            "delta_seconds": self.delta_seconds,
            "within_window": self.within_window,
            "a_at": self.a_at,
            "b_at": self.b_at,
            "hop_id": self.hop_id,
        }


@dataclass(frozen=True)
class RetrievalCandidateTrace:
    """2.0-B1 T2 (AC3): one retrieval candidate context assembly considered
    for this finding — used or not.

    "Retrieval proposes, assembly decides" (context_assembly.py): every
    candidate the retrieval evidence source proposed gets exactly one of
    these, so both sides of the decision are visible — not just the ones
    that made it into the finding's narrative.
    """

    chunk_id: str
    used: bool
    decision: str
    reason: Optional[str]
    confidence: Optional[float]
    origin: Optional[str]
    source_system: Optional[str]
    source_artifact: Optional[str]
    content_snippet: Optional[str]
    is_stale: Optional[bool]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "used": self.used,
            "decision": self.decision,
            "reason": self.reason,
            "confidence": self.confidence,
            "origin": self.origin,
            "source_system": self.source_system,
            "source_artifact": self.source_artifact,
            "content_snippet": self.content_snippet,
            "is_stale": self.is_stale,
        }


@dataclass(frozen=True)
class FindingTrace:
    """The full trace graph for one opportunity."""

    opportunity_id: str
    run_id: str
    hops: List[TraceHop]
    joins: List[JoinTrace]
    complete: bool
    truncated: bool = False
    retrieval_candidates: List[RetrievalCandidateTrace] = field(default_factory=list)
    #: Why the chain stops short of a source record, when it does. Present
    #: precisely when ``complete`` is False, so a reviewer looking at a short
    #: chain is told whether provenance is MISSING or the finding is simply thin
    #: — rather than being left to guess from the hop count.
    incomplete_reason: Optional[str] = None

    @property
    def has_chain(self) -> bool:
        """True when there is something to interrogate below the finding itself.

        Deliberately NOT the same question as :attr:`complete`. This one answers
        "is there a panel to render?"; ``complete`` answers AC1's much stricter
        "does the chain terminate in source records?". Conflating them hides a
        real chain whenever its provenance is incomplete — which is exactly the
        wrong direction, since an incomplete chain is the one a reviewer most
        needs to see.
        """
        return len(self.hops) > 1

    def to_dict(self) -> Dict[str, Any]:
        used = sum(1 for c in self.retrieval_candidates if c.used)
        return {
            "opportunity_id": self.opportunity_id,
            "run_id": self.run_id,
            "hops": [hop.to_dict() for hop in self.hops],
            "joins": [join.to_dict() for join in self.joins],
            "hop_count": len(self.hops),
            "join_count": len(self.joins),
            "complete": self.complete,
            "incomplete_reason": self.incomplete_reason,
            "has_chain": self.has_chain,
            "truncated": self.truncated,
            "retrieval_candidates": [c.to_dict() for c in self.retrieval_candidates],
            "retrieval_candidates_used_count": used,
            "retrieval_candidates_unused_count": len(self.retrieval_candidates) - used,
        }


def _empty_trace(opportunity: Any, run_id: Any) -> FindingTrace:
    opp_id = ""
    if isinstance(opportunity, Mapping):
        opp_id = str(opportunity.get("id") or "")
    return FindingTrace(
        opportunity_id=opp_id,
        run_id=str(run_id or ""),
        hops=[],
        joins=[],
        complete=False,
        truncated=False,
        retrieval_candidates=[],
        incomplete_reason=REASON_NO_TRACE,
    )


def _retrieval_candidate_traces(
    candidates: Optional[Sequence[Mapping[str, Any]]],
) -> List[RetrievalCandidateTrace]:
    """Build RetrievalCandidateTrace entries from stored candidate dicts
    (app.retrieval_trace's persisted shape). Skips malformed entries rather
    than raising — a trace degrades, it never breaks."""
    result: List[RetrievalCandidateTrace] = []
    for candidate in candidates or ():
        if not isinstance(candidate, Mapping):
            continue
        chunk_id = candidate.get("chunk_id")
        if not chunk_id:
            continue
        decision = candidate.get("decision")
        used = candidate.get("used")
        if used is None:
            used = decision == "included"
        result.append(RetrievalCandidateTrace(
            chunk_id=str(chunk_id),
            used=bool(used),
            decision=str(decision or ("included" if used else "excluded")),
            reason=candidate.get("reason"),
            confidence=candidate.get("confidence"),
            origin=candidate.get("origin"),
            source_system=candidate.get("source_system"),
            source_artifact=candidate.get("source_artifact"),
            content_snippet=candidate.get("content_snippet"),
            is_stale=candidate.get("is_stale"),
        ))
    return result


# ── Hop builders ─────────────────────────────────────────────────────────────


def _finding_hop(
    opportunity: Mapping[str, Any],
    run_id: str,
    finding_hop_id: str,
    evidence_items: Sequence[Mapping[str, Any]],
    contract: Optional[Mapping[str, Any]],
    run_completed_at: Optional[str],
) -> TraceHop:
    inferred_any = any(
        _origin_of(ev.get("provenanceType")) == ORIGIN_INFERRED for ev in evidence_items
    )
    systems: List[str] = []
    if contract:
        source_trace = contract.get("source_trace")
        if isinstance(source_trace, Mapping):
            systems = [str(s) for s in (source_trace.get("systems") or []) if s]
    if not systems:
        systems = sorted({
            connector
            for ev in evidence_items
            if (connector := _connector_for_source(ev.get("source")))
        })
    return TraceHop(
        hop_id=finding_hop_id,
        hop_type=HOP_FINDING,
        label=str(opportunity.get("title") or opportunity.get("id") or "finding"),
        origin=ORIGIN_INFERRED if inferred_any else ORIGIN_OBSERVED,
        connector=",".join(systems) if systems else None,
        run_id=run_id,
        timestamp=run_completed_at,
        from_hop_id=None,
        detail={
            "opportunity_id": opportunity.get("id"),
            "pack_id": opportunity.get("packId"),
            "pack_version": opportunity.get("packVersion"),
            "corroboration_rule_ids": list(opportunity.get("corroboration_rule_ids") or []),
        },
    )


def _evidence_hop(ev: Mapping[str, Any], run_id: str, finding_hop_id: str) -> TraceHop:
    ev_id = str(ev.get("id") or "")
    return TraceHop(
        hop_id=f"evidence:{ev_id}",
        hop_type=HOP_EVIDENCE,
        label=str(ev.get("title") or ev.get("evidenceType") or ev_id or "evidence"),
        origin=_origin_of(ev.get("provenanceType")),
        connector=_connector_for_source(ev.get("source")),
        run_id=run_id,
        timestamp=ev.get("tsLabel") or None,
        from_hop_id=finding_hop_id,
        detail={
            "evidence_id": ev_id,
            "evidence_type": ev.get("evidenceType"),
            "confidence": ev.get("confidence"),
            "pack_id": ev.get("packId"),
            "detector_id": ev.get("detectorId"),
        },
    )


def _pointer_hop(
    pointer: Mapping[str, Any], run_id: str, *, index: int, parent_hop_id: str
) -> TraceHop:
    artifact = str(pointer.get("source_artifact") or f"pointer_{index}")
    system = str(pointer.get("source_system") or "") or None
    return TraceHop(
        hop_id=f"source_record:{system or 'unknown'}:{artifact}:{index}",
        hop_type=HOP_SOURCE_RECORD,
        label=artifact,
        origin=_origin_of(pointer.get("origin")),
        connector=system,
        run_id=run_id,
        timestamp=pointer.get("source_timestamp") or None,
        from_hop_id=parent_hop_id,
        detail={
            "source_artifact": artifact,
            "extraction_job_id": pointer.get("extraction_job_id"),
            "chunk_id": pointer.get("chunk_id"),
            "retrieval_result_id": pointer.get("retrieval_result_id"),
            "confidence": pointer.get("confidence"),
        },
    )


def _artifact_hop(
    artifact: Mapping[str, Any],
    run_id: str,
    *,
    index: int,
    parent_hop_id: str,
    systems: Sequence[str],
) -> TraceHop:
    artifact_type = str(artifact.get("type") or "artifact")
    artifact_id = str(artifact.get("id") or f"artifact_{index}")
    connector = _ARTIFACT_TYPE_CONNECTOR.get(artifact_type) or (systems[0] if systems else None)
    detail = {k: v for k, v in artifact.items() if k not in ("type", "id")}
    detail["artifact_type"] = artifact_type
    detail["artifact_id"] = artifact_id
    return TraceHop(
        hop_id=f"source_record:{artifact_type}:{artifact_id}:{index}",
        hop_type=HOP_SOURCE_RECORD,
        label=f"{artifact_type}:{artifact_id}",
        origin=ORIGIN_OBSERVED,
        connector=connector,
        run_id=run_id,
        timestamp=None,
        from_hop_id=parent_hop_id,
        detail=detail,
    )


def _within_window_joins(
    correlation_windows: Optional[Sequence[Mapping[str, Any]]],
) -> List[Mapping[str, Any]]:
    """Defence-in-depth mirror of cloud_ops_finding._filter_correlation_windows.

    2.0-B1 AC2: a join outside its correlation window must never surface as a
    claim this trace displays. The producing pack already filters before
    persistence; this module filters again on the way out so the guarantee
    holds independent of what a caller hands in.

    Fail CLOSED on the flag: only a literal ``True`` keeps an entry, not merely a
    truthy one. Truthiness would admit ``"false"`` / ``"no"`` — a real hazard for
    a value that survives a JSON round trip or a hand-written fixture — and the
    point of this layer is that an out-of-window join cannot appear even when the
    caller is wrong. Every real producer (``WindowJoin.within``,
    ``cloud_ops_finding._filter_correlation_windows``) already emits a bool
    (2.0-B1 T7 QA finding).
    """
    kept: List[Mapping[str, Any]] = []
    for window in correlation_windows or ():
        if isinstance(window, Mapping) and window.get("within_window") is True:
            kept.append(window)
    return kept


def _joins_from_contract(
    contract: Optional[Mapping[str, Any]],
    artifact_hops_by_type: Mapping[str, TraceHop],
) -> List[JoinTrace]:
    if not contract:
        return []
    corroboration = contract.get("corroboration")
    if not isinstance(corroboration, Mapping):
        return []
    windows = _within_window_joins(corroboration.get("correlation_windows"))
    event_hop = artifact_hops_by_type.get("event_signature")
    joins: List[JoinTrace] = []
    for window in windows:
        joins.append(JoinTrace(
            join_type=str(window.get("join_type") or ""),
            window_seconds=window.get("window_seconds"),
            delta_seconds=window.get("delta_seconds"),
            within_window=True,
            a_at=window.get("a_at"),
            b_at=window.get("b_at"),
            hop_id=event_hop.hop_id if event_hop is not None else None,
        ))
    return joins


# ── Public API ───────────────────────────────────────────────────────────────


def build_finding_trace(
    opportunity: Mapping[str, Any],
    run_id: str,
    *,
    evidence_items: Optional[Sequence[Mapping[str, Any]]] = None,
    pointers: Optional[Sequence[Mapping[str, Any]]] = None,
    run_completed_at: Optional[str] = None,
    retrieval_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
) -> FindingTrace:
    """Build the full provenance chain for one finding.

    Parameters
    ----------
    opportunity : the stored (Track-A shaped) opportunity dict — at minimum
        'id'; 'evidenceIds' and 'findingContract' are read when present.
    run_id : the run the opportunity belongs to.
    evidence_items : the run's evidence rows matching opportunity['evidenceIds']
        (any order — resolved by id). None/[] when the opportunity's evidence
        cannot be located (e.g. a cloud_ops finding with no build_evidence()
        output) — the finding_contract's source_trace artifacts still supply
        source-record hops, so the chain does not go empty.
    pointers : the opportunity's stored EvidencePointer dicts (see
        app.evidence_pointers.get_evidence_pointers_for_opportunity), used to
        add the source-record hop beneath each evidence hop. Each pointer's
        'detector_evidence_id' links it back to the evidence hop it was
        derived from; a pointer with no match attaches directly under the
        finding.
    run_completed_at : optional ISO timestamp stamped on the finding root hop.
    retrieval_candidates : (2.0-B1 T2 / AC3) this opportunity's stored
        retrieval-candidate records (see
        app.retrieval_trace.get_retrieval_candidates_for_opportunity) — every
        candidate context assembly considered, used and unused alike, so
        "retrieval proposes, assembly decides" is visible on both sides.

    Never raises: any unexpected shape degrades to the emptiest valid trace
    (the finding hop alone, or nothing at all) rather than propagating an
    exception, matching graph_context_builder.py's posture.
    """
    try:
        return _build_finding_trace(
            opportunity,
            run_id,
            evidence_items=evidence_items or (),
            pointers=pointers or (),
            run_completed_at=run_completed_at,
            retrieval_candidates=retrieval_candidates or (),
        )
    except Exception as exc:  # noqa: BLE001 — trace building is advisory.
        logger.warning(
            "trace_graph: build_finding_trace failed for run=%s opp=%s: %s",
            run_id,
            (opportunity or {}).get("id") if isinstance(opportunity, Mapping) else None,
            exc,
        )
        return _empty_trace(opportunity, run_id)


def _build_finding_trace(
    opportunity: Mapping[str, Any],
    run_id: str,
    *,
    evidence_items: Sequence[Mapping[str, Any]],
    pointers: Sequence[Mapping[str, Any]],
    run_completed_at: Optional[str],
    retrieval_candidates: Sequence[Mapping[str, Any]] = (),
) -> FindingTrace:
    opp_id = str(opportunity.get("id") or "")
    run_id = str(run_id or "")
    finding_hop_id = f"finding:{opp_id}"

    # Resolve evidence rows in the opportunity's own evidenceIds order so the
    # chain reads in the same order the opportunity itself presents evidence.
    evidence_by_id = {
        str(ev.get("id")): ev
        for ev in evidence_items
        if isinstance(ev, Mapping) and ev.get("id")
    }
    ordered_evidence_ids = [
        str(eid) for eid in (opportunity.get("evidenceIds") or []) if eid
    ]
    if not ordered_evidence_ids:
        # Run-internal opp shape carries evidence inline rather than by id.
        inline = opportunity.get("evidence")
        if isinstance(inline, list):
            for ev in inline:
                if isinstance(ev, Mapping) and ev.get("id"):
                    evidence_by_id.setdefault(str(ev["id"]), ev)
                    ordered_evidence_ids.append(str(ev["id"]))
    resolved_evidence = [
        evidence_by_id[eid] for eid in ordered_evidence_ids if eid in evidence_by_id
    ]

    contract = opportunity.get("findingContract")
    if not isinstance(contract, Mapping):
        contract = None

    hops: List[TraceHop] = [
        _finding_hop(
            opportunity, run_id, finding_hop_id, resolved_evidence, contract, run_completed_at
        )
    ]

    evidence_hop_by_id: Dict[str, TraceHop] = {}
    for eid in ordered_evidence_ids:
        ev = evidence_by_id.get(eid)
        if ev is None:
            continue
        hop = _evidence_hop(ev, run_id, finding_hop_id)
        hops.append(hop)
        evidence_hop_by_id[eid] = hop

    for index, pointer in enumerate(pointers):
        if not isinstance(pointer, Mapping):
            continue
        linked_evidence_id = pointer.get("detector_evidence_id")
        parent = evidence_hop_by_id.get(str(linked_evidence_id)) if linked_evidence_id else None
        hops.append(
            _pointer_hop(
                pointer,
                run_id,
                index=index,
                parent_hop_id=(parent.hop_id if parent is not None else finding_hop_id),
            )
        )

    artifact_hops_by_type: Dict[str, TraceHop] = {}
    if contract is not None:
        source_trace = contract.get("source_trace")
        if isinstance(source_trace, Mapping):
            systems = [str(s) for s in (source_trace.get("systems") or []) if s]
            artifacts = source_trace.get("artifacts")
            if isinstance(artifacts, list):
                for index, artifact in enumerate(artifacts):
                    if not isinstance(artifact, Mapping):
                        continue
                    hop = _artifact_hop(
                        artifact,
                        run_id,
                        index=index,
                        parent_hop_id=finding_hop_id,
                        systems=systems,
                    )
                    hops.append(hop)
                    artifact_type = str(artifact.get("type") or "")
                    artifact_hops_by_type.setdefault(artifact_type, hop)

    joins = _joins_from_contract(contract, artifact_hops_by_type)

    truncated = len(hops) > _MAX_HOPS
    if truncated:
        hops = hops[:_MAX_HOPS]

    # AC1 is "a complete chain TERMINATING IN SOURCE RECORDS", so that is what
    # `complete` measures. It used to be `len(hops) > 1`, which reported a
    # finding->evidence chain as complete even though it never reached an
    # originating record — the reviewer then had no way to tell a missing
    # provenance tier from a genuinely thin finding. Whatever falls short says so.
    reached_source_record = any(h.hop_type == HOP_SOURCE_RECORD for h in hops)
    if reached_source_record:
        incomplete_reason = None
    elif len(hops) > 1:
        incomplete_reason = REASON_NO_SOURCE_RECORD
    else:
        incomplete_reason = REASON_NO_TRACE

    return FindingTrace(
        opportunity_id=opp_id,
        run_id=run_id,
        hops=hops,
        joins=joins,
        complete=reached_source_record,
        truncated=truncated,
        retrieval_candidates=_retrieval_candidate_traces(retrieval_candidates),
        incomplete_reason=incomplete_reason,
    )


def load_finding_trace(run_id: str, opp_id: str) -> Optional[FindingTrace]:
    """DB-aware convenience wrapper: load a run's opportunity/evidence/pointers
    from run-scoped storage and build its trace.

    Returns None when the opportunity cannot be found in the run (mirrors
    routes_sprint4_t6.py's ``_find_stored_opp`` contract — a 404 case, decided
    by the caller). Never raises for any OTHER failure — a storage/format
    problem degrades to an empty-but-valid trace, same as build_finding_trace.

    Does NOT enforce tenancy — the route layer owns the org-boundary check
    (it has the request org context), exactly like
    evidence_pointers.get_evidence_pointers_for_opportunity.
    """
    from .evidence_pointers import get_evidence_pointers_for_opportunity

    opps = db.run_kv_get("opps", run_id, []) or []
    opportunity = next(
        (o for o in opps if isinstance(o, Mapping) and o.get("id") == opp_id), None
    )
    if opportunity is None:
        return None

    evidence_items = db.run_kv_get("evidence", run_id, []) or []
    try:
        pointers = get_evidence_pointers_for_opportunity(run_id, opp_id)
    except Exception as exc:  # noqa: BLE001 — pointers are advisory here.
        logger.debug(
            "trace_graph: pointer load failed for run=%s opp=%s: %s", run_id, opp_id, exc
        )
        pointers = []

    try:
        from .retrieval_trace import get_retrieval_candidates_for_opportunity

        retrieval_candidates = get_retrieval_candidates_for_opportunity(run_id, opp_id)
    except Exception as exc:  # noqa: BLE001 — retrieval candidates are advisory here.
        logger.debug(
            "trace_graph: retrieval-candidate load failed for run=%s opp=%s: %s",
            run_id, opp_id, exc,
        )
        retrieval_candidates = []

    run = None
    try:
        run = db.get_run(run_id)
    except Exception:
        run = None
    run_completed_at = None
    if isinstance(run, Mapping):
        run_completed_at = run.get("completedAt") or run.get("completed_at")

    return build_finding_trace(
        opportunity,
        run_id,
        evidence_items=evidence_items,
        pointers=pointers,
        run_completed_at=run_completed_at,
        retrieval_candidates=retrieval_candidates,
    )


__all__ = [
    "HOP_FINDING",
    "HOP_EVIDENCE",
    "HOP_SOURCE_RECORD",
    "ORIGIN_OBSERVED",
    "ORIGIN_INFERRED",
    "TraceHop",
    "JoinTrace",
    "RetrievalCandidateTrace",
    "FindingTrace",
    "build_finding_trace",
    "load_finding_trace",
]
