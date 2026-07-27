"""
SHARED-2 — Sprint 5 — nCino Lending Entity Extension
GET /api/runs/{runId}/normalization

Returns normalization mapping rows for a specific run.

Data source priority:
  1. Run KV key "normalization" — written by normalization_enrichment.py
     when enrich_ambiguous_mappings() is wired into the runner.
  2. Derived from run evidence and opportunity data — available whenever a
     detector fired, producing real rows from actual ingested data.
  3. Derived from the run's persisted signal snapshots (the `signal_snapshots`
     table, written by temporal.snapshot_signals for EVERY detector evaluation,
     fired or not). Tiers 1 and 2 both depend on a detector having FIRED: an
     evidence item only exists behind an opportunity. A run that ingested real
     data and evaluated it — but crossed no threshold — therefore produced no
     rows at all under tiers 1-2, and Source Intelligence rendered it exactly
     like a run that ingested nothing. Tier 3 closes that gap from data the
     pipeline already persists: it contributes rows ONLY for sources tiers 1-2
     did not already cover, so a source that produced evidence keeps its
     evidence-derived rows unchanged.

This endpoint replaces the frontend mockMappings.json. The context
(NormalizationContext.tsx) calls this endpoint using the current runId.

Wire-in (main.py):
    from .routes_normalization import register_normalization_routes
    register_normalization_routes(app)
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from .security import require_auth
from .rbac import require_role
from . import db

from .normalization_enrichment import KV_NORMALIZATION  # shared key — Issue 1 fix
from .source_keys import source_key_for

KV_EVIDENCE = "evidence"

# Internal-only marker on an evidence-derived row recording the
# (sourceSystem, detector) pair it approximates. Stripped before serialization —
# it is never part of the response contract.
_DETECTOR_KEY = "_detectorKey"

# ── Response models ───────────────────────────────────────────────────────────

class MappingRowOut(BaseModel):
    id: str
    sourceSystem: str
    sourceType: str
    sourceField: str
    commonEntity: str
    commonField: str
    status: str        # MAPPED | UNMAPPED | AMBIGUOUS
    confidence: str    # HIGH | MEDIUM | LOW
    sampleValues: List[str]
    notes: str = ""


class NormalizationResponse(BaseModel):
    runId: str
    rows: List[MappingRowOut]
    counts: Dict[str, int]
    source: str   # "stored" | "derived" — tells frontend how data was produced


# ── Entity type derivation ────────────────────────────────────────────────────
# SHARED-2 Issue 3 fix: derive lending entity type from detector_id, not from
# fragile source string conventions. The detector_id is set explicitly by each
# nCino detector and is always present on evidence items.

_DETECTOR_ENTITY_MAP: dict = {
    # nCino lending detectors → lending canonical entity types
    "LOAN_ORIGINATION_ROUTING_FRICTION": "Loan",
    "STAGE_DURATION_OVERRUN":            "Loan",
    "COVENANT_TRACKING_GAP":             "Covenant",
    "CHECKLIST_BOTTLENECK":              "Checklist",
    "SPREADING_BOTTLENECK":              "SpreadPeriod",
    "APPROVAL_BOTTLENECK":               "LendingApproval",
    # Service Cloud detectors → Service Cloud entity types (original)
    "CASE_ROUTING_FRICTION":             "Workflow",
    "REPEATED_ESCALATIONS":              "Workflow",
    "PERMISSION_BOTTLENECK":             "Application",
    "REPETITIVE_AUTOMATION":             "Service",
}

_SOURCE_ENTITY_MAP: dict = {
    # Fallback: source-level entity type when no detector_id match
    "Salesforce":  "Workflow",
    "ServiceNow":  "Application",
    "Jira":        "Workflow",
    "Databricks":  "Service",
}


def _entity_from_detector(detector_id_or_ev_type: str, source: str) -> str:
    """
    Derive canonical entity type from detector_id (preferred) or source (fallback).

    Issue 3 fix: detector_id is explicit and not fragile. Source-string
    conventions (nCino:Loan etc.) are no longer required.
    """
    # Try detector_id first (explicit, reliable)
    entity = _DETECTOR_ENTITY_MAP.get(detector_id_or_ev_type)
    if entity:
        return entity
    # Fall back to source-level map (Service Cloud behaviour preserved)
    return _SOURCE_ENTITY_MAP.get(source, "DataObject")


# ── Derivation from evidence ──────────────────────────────────────────────────

def _derive_from_evidence(run_id: str) -> List[Dict[str, Any]]:
    """
    Derive normalization rows from the run's evidence items.
    Evidence items have source, evidenceType, title, snippet — enough
    to produce representative mapping rows showing what each source
    contributed to the run.

    This is the fallback when no stored normalization data exists.
    Rows are MAPPED with confidence derived from the evidence confidence.
    """
    evidence: List[Dict[str, Any]] = db.run_kv_get(KV_EVIDENCE, run_id, [])
    if not evidence:
        return []

    rows: List[Dict[str, Any]] = []
    seen: set = set()

    for ev in evidence:
        source = ev.get("source", "")
        # Issue 2 fix: evidenceType is always "Metric"/"Log" — not a detector ID.
        # Read detectorId if present (set by nCino evidence enrichment path),
        # fall back to evidenceType for backward compat with Service Cloud evidence.
        ev_type = ev.get("detectorId") or ev.get("evidenceType", "Metric")
        title = ev.get("title", "")
        confidence = ev.get("confidence", "MEDIUM")
        ev_id = ev.get("id", "")

        # Derive a representative field name from the evidence title
        # Limit to one row per source+type combination to avoid flooding
        key = (source, ev_type)
        if key in seen:
            continue
        seen.add(key)

        # Carried so the caller can drop this approximation when the persisted
        # snapshots describe the SAME (source, detector) at their real grain.
        # Stripped before serialization — never part of the response contract.
        detector_key = (source, ev_type)

        # Map evidenceType to a sourceType label
        source_type_map = {
            "Metric":  "CRM" if source == "Salesforce" else "ITSM" if source == "ServiceNow" else "Tickets",
            "Log":     "Events",
            "Event":   "Events",
            "Email":   "Email",
            "Ticket":  "Tickets",
            "Chat":    "Chat",
            "Doc":     "Documentation",
        }
        source_type = source_type_map.get(ev_type, ev_type)

        # Derive entity type.
        # SHARED-2 Issue 3 fix: use detector_id (from evidenceType field) to
        # derive lending entity type. This is explicit and not fragile — the
        # detector_id is set by the nCino detector and does not depend on
        # source string conventions.
        entity = _entity_from_detector(ev_type, source)

        rows.append({
            "id":           f"norm_{ev_id}",
            "sourceSystem": source,
            "sourceType":   source_type,
            "sourceField":  f"{source.lower()}.{ev_type.lower()}_signal",
            "commonEntity": entity,
            "commonField":  f"{entity}.{ev_type.lower()}",
            "status":       "MAPPED",
            "confidence":   confidence,
            "sampleValues": [],
            "notes":        f"Derived from evidence: {title[:60]}",
            _DETECTOR_KEY:  detector_key,
        })
    return rows


# ── Derivation from persisted signal snapshots ────────────────────────────────


def _run_org_id(run: Dict[str, Any]) -> str:
    """The org a run belongs to, matching how every other run reader resolves it."""
    inputs = run.get("inputs") or {}
    input_org = None
    if isinstance(inputs, dict):
        input_org = inputs.get("orgId") or inputs.get("org_id")
    return str(run.get("orgId") or run.get("org_id") or input_org or "")


def _derive_from_signal_snapshots(
    run_id: str,
    org_id: str,
) -> List[Dict[str, Any]]:
    """Derive normalization rows from the run's persisted signal snapshots.

    Every detector evaluation of a run is persisted to ``signal_snapshots`` by
    ``temporal.snapshot_signals`` — including evaluations that did NOT fire —
    with the ``signal_source`` the detector explicitly declared. That table is
    therefore the authoritative per-source record of "what this run actually read
    and evaluated", independent of whether anything crossed a threshold.

    Rows are emitted at the table's own grain (one per detector metric) and are
    marked MAPPED/HIGH because the source→signal association is DECLARED by the
    detector (``signal_source`` + ``signal_key``), not inferred from a string
    convention — there is nothing ambiguous to resolve. ``sampleValues`` carries
    the real observed ``metric_value``; no value is invented.

    Every snapshot is emitted; the CALLER removes the evidence-derived rows these
    supersede (see ``_merge_snapshot_rows``), so nothing is double-counted and no
    source loses its finer-grained record.

    A read failure degrades to no extra rows: the endpoint must keep serving the
    evidence-derived view rather than 500.
    """
    if not org_id:
        return []

    try:
        from .temporal import get_run_signal_rows

        snapshots = get_run_signal_rows(org_id, run_id)
    except Exception:  # noqa: BLE001 — a snapshot read must never break the endpoint
        return []

    rows: List[Dict[str, Any]] = []
    seen: set = set()

    for snap in snapshots:
        signal_source = str(snap.get("signal_source") or "").strip()
        if not signal_source:
            continue

        source_system = source_key_for(signal_source)
        detector_id = str(snap.get("detector_id") or "").strip()
        metric_name = str(snap.get("metric_name") or "").strip()
        if not detector_id or not metric_name:
            continue

        key = (source_system, detector_id, metric_name)
        if key in seen:
            continue
        seen.add(key)

        metric_value = snap.get("metric_value")
        rows.append({
            "id":           f"norm_sig_{snap.get('id')}",
            "sourceSystem": source_system,
            "sourceType":   "Detector Signal",
            "sourceField":  f"{signal_source}.{metric_name}",
            "commonEntity": "Signal",
            "commonField":  f"{detector_id}.{metric_name}",
            "status":       "MAPPED",
            "confidence":   "HIGH",
            "sampleValues": [] if metric_value is None else [str(metric_value)],
            "notes":        (
                f"Signal read from {source_system} and evaluated by "
                f"{detector_id}{' (threshold met)' if snap.get('fired') else ''}."
            ),
            _DETECTOR_KEY: (source_system, detector_id),
        })

    return rows


def _merge_snapshot_rows(
    prior_rows: List[Dict[str, Any]],
    snapshot_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Add snapshot rows ONLY for sources tiers 1-2 did not already cover.

    Tier 3 exists to speak for a source that tiers 1-2 CANNOT describe: evidence
    hangs off an opportunity, so a run that read and evaluated real data below
    threshold produced no evidence at all and rendered exactly like a run that
    ingested nothing. Snapshots close that gap.

    It is a gap-filler, not a replacement. Tiers 1-2 remain authoritative for any
    source they already describe, and a source they cover keeps its rows
    unchanged — the contract stated in this module's docstring.

    This is per SOURCE, not per (source, detector), and that distinction is the
    whole point. Snapshots are recorded at one row per (source, detector, METRIC)
    while tiers 1-2 sit at one row per (source, detector): letting snapshots
    supersede a covered source therefore does not swap like for like, it swaps a
    handful of summary rows for every metric of every detector that source ran,
    which is a different — and far noisier — statement than this page makes.
    Suppressing the whole source keeps the two grains from ever being mixed in
    one response.

    Every prior row survives unconditionally, including tier-1 stored field
    mappings carrying the AMBIGUOUS/UNMAPPED states the review panel needs.
    """
    covered_sources = {
        str(row.get("sourceSystem") or "")
        for row in prior_rows
        if row.get("sourceSystem")
    }
    gap_filling = [
        row for row in snapshot_rows
        if str(row.get("sourceSystem") or "") not in covered_sources
    ]
    return list(prior_rows) + gap_filling


# ── Route registration ────────────────────────────────────────────────────────

def register_normalization_routes(app) -> None:

    @app.get(
        "/api/runs/{run_id}/normalization",
        response_model=NormalizationResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        tags=["normalization"],
    )
    def get_run_normalization(run_id: str) -> NormalizationResponse:
        """
        Return normalization mapping rows for a run.

        Priority:
          1. Stored normalization data (from enrich_ambiguous_mappings)
          2. Derived from evidence items in the run

        Returns 404 if the run does not exist.
        """
        run = db.run_get(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found",
            )

        # Priority 1: stored normalization data
        stored = db.run_kv_get(KV_NORMALIZATION, run_id, None)
        # Issue 1 fix: stored may be a dict {"rows": [...], ...metadata}
        # or a legacy list. Handle both.
        stored_rows = None
        if stored:
            if isinstance(stored, dict) and stored.get("rows"):
                stored_rows = stored["rows"]
            elif isinstance(stored, list) and len(stored) > 0:
                stored_rows = stored  # legacy list shape

        # Source Intelligence v3: derive from evidence when stored
        # normalization does not cover every evidence source.
        if stored_rows:
            stored_sources = {r.get("sourceSystem", "") for r in stored_rows}
            evidence_check = db.run_kv_get(KV_EVIDENCE, run_id, []) or []
            evidence_sources = {e.get("source", "") for e in evidence_check}
            extra_sources = evidence_sources - stored_sources - {""}
            if extra_sources:
                stored_rows = None

        if stored_rows:
            rows = stored_rows
            data_source = "stored"
        else:
            # Priority 2: derive from evidence
            rows = _derive_from_evidence(run_id)
            data_source = "derived"

        # Priority 3: the run's persisted signal snapshots — the complete, real
        # record of what each source contributed. Tiers 1-2 can only speak for a
        # source once a detector FIRED on it (evidence hangs off an opportunity),
        # so a run that read and evaluated real data below threshold yielded
        # nothing at all, and a source whose detectors DID fire was reported at the
        # coarse one-row-per-detector grain of evidence rather than its real signal
        # count. _merge_snapshot_rows resolves the overlap.
        snapshot_rows = _derive_from_signal_snapshots(run_id, _run_org_id(run))
        if snapshot_rows:
            rows = _merge_snapshot_rows(list(rows), snapshot_rows)
            # `source` stays inside its existing "stored" | "derived" vocabulary
            # (the frontend types it as that union) — snapshot rows are derived
            # from persisted run data exactly as evidence-derived rows are, so no
            # contract change is required. "stored" is preserved when stored rows
            # were used, so the flag keeps meaning what it always meant.
            if data_source != "stored":
                data_source = "derived"

        # The detector key is an internal merge marker, never part of the wire
        # contract — drop it before the response model is built.
        rows = [
            {k: v for k, v in row.items() if k != _DETECTOR_KEY}
            for row in rows
        ]

        # Compute counts
        counts: Dict[str, int] = {"MAPPED": 0, "UNMAPPED": 0, "AMBIGUOUS": 0}
        for row in rows:
            status = row.get("status", "UNMAPPED")
            if status in counts:
                counts[status] += 1

        return NormalizationResponse(
            runId=run_id,
            rows=[MappingRowOut(**r) for r in rows],
            counts=counts,
            source=data_source,
        )
