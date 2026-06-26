"""
R16-B1 (T6) — Evidence Pointer storage + retrieval.

Provenance is captured when a finding is formed (T1/T2), but capture is not
enough: users and later features must be able to walk from an opportunity back
to the source records that produced it. This module is that queryable spine —
the data the *full evidence trace* (release 1.9) will render.

What this module owns (T6):
  - A run-scoped store of evidence pointers, keyed by opportunity id.
  - A retrieval function that, given a run + opportunity, returns the source
    trail (at minimum source_system + source_artifact + source_timestamp).
  - Org-scoped access: pointers for a run belonging to org A are never returned
    to a request scoped to org B (provenance points back to real business
    systems, so the tenant boundary is load-bearing).

What this module does NOT own:
  - The canonical EvidencePointer model and its is_valid() rule. Those belong to
    T1 (backend/app/provenance.py). That module does not exist on this branch
    yet, so T6 defines a structurally-identical pointer shape here
    (POINTER_FIELDS mirrors doc Section 1 exactly) and reads through
    _evidence_pointer_factory(), which prefers provenance.EvidencePointer when
    it lands. When T1 merges, this storage layer adopts it with no schema change
    — the stored dict shape is already the spine's shape.
  - Capturing pointers at entity/relationship/enrichment creation time (T2).
    Until T2 lands there is no first-class pointer recorded during the run, so
    T6 DERIVES observed pointers from the evidence already attached to each
    opportunity (its source system, the detector artifact, and the run
    timestamp). Derivation is clearly separated (build_pointers_for_opportunity)
    so it can be replaced by real captured pointers without touching storage or
    retrieval.

Extensibility (AC8): the pointer dict always carries chunk_id and
retrieval_result_id keys, null in 1.6, ready for retrieval (1.8) to populate
without a schema migration.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import db

logger = logging.getLogger(__name__)

# Run-scoped KV namespace for stored pointers (parallel to "opps"/"evidence").
KV_EVIDENCE_POINTERS = "evidence_pointers"

# The pointer's mandatory spine + extensible detail, mirroring doc Section 1 and
# T1's EvidencePointer. Kept here as the single description of the stored shape
# so storage, retrieval, and tests agree even before provenance.py exists.
POINTER_FIELDS: tuple[str, ...] = (
    # ---- mandatory spine (populated now, in 1.6) ----
    "source_system",       # 'salesforce', 'servicenow', 'jira', ...
    "source_artifact",     # stable id of the source record/message/doc
    "source_timestamp",    # when the source artifact was observed (UTC)
    "origin",              # 'observed' | 'inferred'
    "extraction_job_id",   # required WHEN origin='inferred'
    # ---- extensible detail (null in 1.6; filled as modules land) ----
    "chunk_id",            # set by retrieval (1.8)
    "retrieval_result_id",  # set by retrieval (1.8)
    "detector_evidence_id",  # set by the detector layer later
    "confidence",          # optional float
)

# Canonicalise the human source label evidence objects carry ("Salesforce")
# into the connector-style system id the rest of the provenance spine uses
# ("salesforce"). Unknown labels fall through lowercased.
_SOURCE_SYSTEM_BY_LABEL: Dict[str, str] = {
    "salesforce": "salesforce",
    "servicenow": "servicenow",
    "jira": "jira",
    "github": "github",
}


def _pointer(
    *,
    source_system: str,
    source_artifact: str,
    source_timestamp: str,
    origin: str = "observed",
    extraction_job_id: Optional[str] = None,
    detector_evidence_id: Optional[str] = None,
    confidence: Optional[float] = None,
) -> Dict[str, Any]:
    """Build one pointer dict with the full spine + null extensible fields.

    The extensible retrieval fields (chunk_id, retrieval_result_id) are always
    present and None here (AC8). detector_evidence_id is allowed because T6 can
    link the pointer to the display-evidence row it was derived from; it stays
    None when there is nothing to link.
    """
    return {
        "source_system": source_system,
        "source_artifact": source_artifact,
        "source_timestamp": source_timestamp,
        "origin": origin,
        "extraction_job_id": extraction_job_id,
        # extensible detail — present-but-null in 1.6 (AC8)
        "chunk_id": None,
        "retrieval_result_id": None,
        "detector_evidence_id": detector_evidence_id,
        "confidence": confidence,
    }


def _is_valid_pointer(pointer: Dict[str, Any]) -> bool:
    """Validate the mandatory spine, mirroring T1 EvidencePointer.is_valid().

    Prefers provenance.EvidencePointer.is_valid() when T1 has landed so the two
    layers can never disagree on what 'valid' means; falls back to the inline
    rule (the same one) when provenance.py is absent.
    """
    try:  # adopt T1's canonical validator the moment it exists
        from .provenance import EvidencePointer  # type: ignore

        return EvidencePointer(
            source_system=pointer.get("source_system", ""),
            source_artifact=pointer.get("source_artifact", ""),
            source_timestamp=pointer.get("source_timestamp", ""),
            origin=pointer.get("origin", ""),
            extraction_job_id=pointer.get("extraction_job_id"),
        ).is_valid()
    except Exception:
        pass

    if not (
        pointer.get("source_system")
        and pointer.get("source_artifact")
        and pointer.get("source_timestamp")
        and pointer.get("origin")
    ):
        return False
    if pointer.get("origin") == "inferred" and not pointer.get("extraction_job_id"):
        return False
    return True


def _source_system_for(label: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """Normalise an evidence 'source' label to a connector-style system id."""
    for candidate in (label, fallback):
        if candidate and str(candidate).strip():
            key = str(candidate).strip().lower()
            return _SOURCE_SYSTEM_BY_LABEL.get(key, key)
    return None


def build_pointers_for_opportunity(
    opp: Dict[str, Any],
    *,
    evidence_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    run_completed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Derive observed evidence pointers for one opportunity.

    Until T2 records first-class pointers during the run, the queryable trail is
    derived from what the opportunity already carries: each attached evidence
    item names its source system, and the detector firing identifies the source
    artifact. Every derived pointer is origin='observed' — none of this is
    inferred content.

    Two opp layouts are supported:
      - The run-shaped opp carries its evidence inline under 'evidence' and its
        detector under 'detector_id' / 'signal_source'.
      - The Track-A-shaped opp (what materialization persists) keeps only
        'evidenceIds' and stashes the detector under '_debug'; pass the run's
        flattened evidence list as ``evidence_by_id`` to resolve those ids.
    """
    pointers: List[Dict[str, Any]] = []

    debug = opp.get("_debug") if isinstance(opp.get("_debug"), dict) else {}
    detector_id = opp.get("detector_id") or debug.get("detector_id") or ""
    opp_signal_source = opp.get("signal_source") or debug.get("signal_source") or ""
    ts = run_completed_at or ""

    # Resolve the evidence rows: inline list if present, else look up by id in
    # the run's flattened evidence (Track-A layout).
    evidence_items = opp.get("evidence")
    if not (isinstance(evidence_items, list) and evidence_items) and evidence_by_id:
        evidence_items = [
            evidence_by_id[eid]
            for eid in (opp.get("evidenceIds") or [])
            if eid in evidence_by_id
        ]

    if isinstance(evidence_items, list) and evidence_items:
        for ev in evidence_items:
            if not isinstance(ev, dict):
                continue
            system = _source_system_for(ev.get("source"), opp_signal_source)
            artifact = ev.get("id") or detector_id
            # Per-evidence UTC label is the closest observation timestamp we
            # have; fall back to the run completion time.
            timestamp = ev.get("tsLabel") or ts
            if not (system and artifact and timestamp):
                continue
            pointer = _pointer(
                source_system=system,
                source_artifact=str(artifact),
                source_timestamp=str(timestamp),
                origin="observed",
                detector_evidence_id=ev.get("id"),
            )
            if _is_valid_pointer(pointer):
                pointers.append(pointer)

    # Fallback: an opportunity with no usable evidence rows still has a detector
    # firing, which is itself an observed source artifact. Without this, such a
    # finding would have no queryable provenance at all.
    if not pointers:
        system = _source_system_for(opp_signal_source, None)
        if system and detector_id and ts:
            pointer = _pointer(
                source_system=system,
                source_artifact=str(detector_id),
                source_timestamp=str(ts),
                origin="observed",
            )
            if _is_valid_pointer(pointer):
                pointers.append(pointer)

    return pointers


def build_pointer_index(
    opps: List[Dict[str, Any]],
    *,
    evidence: Optional[List[Dict[str, Any]]] = None,
    run_completed_at: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Build the {opportunity_id: [pointer, ...]} index for a run's opps.

    ``evidence`` is the run's flattened evidence list (Track-A layout); it is
    indexed by id once and shared across all opps. Opportunities without an id
    are skipped (the index is keyed for retrieval by opportunity id, so an
    unkeyed entry could never be queried back).
    """
    evidence_by_id: Dict[str, Dict[str, Any]] = {
        ev["id"]: ev
        for ev in (evidence or [])
        if isinstance(ev, dict) and ev.get("id")
    }
    index: Dict[str, List[Dict[str, Any]]] = {}
    for opp in opps or []:
        if not isinstance(opp, dict):
            continue
        opp_id = opp.get("id")
        if not opp_id:
            continue
        index[str(opp_id)] = build_pointers_for_opportunity(
            opp, evidence_by_id=evidence_by_id, run_completed_at=run_completed_at
        )
    return index


def store_evidence_pointers(
    run_id: str,
    opps: List[Dict[str, Any]],
    *,
    evidence: Optional[List[Dict[str, Any]]] = None,
    run_completed_at: Optional[str] = None,
) -> int:
    """Persist the per-opportunity pointer index for a run (run-scoped KV).

    Returns the number of opportunities indexed. Never raises — provenance
    storage is additive and must not break materialization; a failure is logged
    and the run proceeds (the trace endpoint then simply returns no pointers).
    """
    try:
        index = build_pointer_index(
            opps, evidence=evidence, run_completed_at=run_completed_at
        )
        db.run_kv_set(KV_EVIDENCE_POINTERS, run_id, index)
        return len(index)
    except Exception as exc:  # noqa: BLE001 — additive, non-blocking.
        logger.warning(
            "evidence pointer storage failed (non-blocking) run=%s: %s", run_id, exc
        )
        return 0


def get_evidence_pointers_for_opportunity(
    run_id: str,
    opp_id: str,
) -> List[Dict[str, Any]]:
    """Return the stored evidence pointers for one opportunity in a run.

    Reads the run-scoped index written by store_evidence_pointers(). Returns an
    empty list when nothing was stored (older runs, or a run whose storage step
    failed). Does NOT enforce tenancy on its own — the route layer owns the
    org-boundary check (it has the request org context); see
    routes_sprint4_t6.get_evidence_trace.
    """
    index = db.run_kv_get(KV_EVIDENCE_POINTERS, run_id, {}) or {}
    if not isinstance(index, dict):
        return []
    pointers = index.get(str(opp_id), [])
    return [p for p in pointers if isinstance(p, dict)] if isinstance(pointers, list) else []
