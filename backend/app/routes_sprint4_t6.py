"""
Sprint 4 T6 — LLM Enrichment Routes  v1.1

Changes from v1.0:
  Fix 4: Removed claim about GET /executive-report/summary endpoint which
          was not implemented. Executive summary is served via the existing
          GET /executive-report endpoint's aiExecutiveSummary field.
  Fix 5: Registration order documented correctly — after T4.
  Fix 6: Fallback OppEnrichment now returns the full model shape with all
          list fields as empty lists, matching the LLM-generated shape.

Adds two endpoints:
  GET /api/runs/{runId}/llm-enrichment
      Returns enrichment status and executive summary for a run.
      Returns available: false (not 404) if enrichment not yet generated.

  GET /api/runs/{runId}/opportunities/{oppId}/enrichment
      Returns LLM fields for a single opportunity.
      Never returns 404 for missing enrichment — always returns usable
      fallback (aiRationale as aiSummary, empty lists for bullet fields).

Wire-in (main.py):
  from .routes_sprint4_t6 import register_sprint4_t6_routes
  register_sprint4_t6_routes(app)

  Add to the registration block after register_sprint4_t4_routes(app).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .security import require_auth
from .rbac import require_role
from . import db
from .llm_enrichment import KV_LLM_ENRICHMENT
from .terminology import apply_terminology, resolve_run_terminology
from .temporal import get_baseline, get_signal_history
from .graph_query import RelationshipSummary, select_relationships_for_opportunity
from database.models.entities import ENTITY_MIN_RUN_COUNT

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────

class EntitySummary(BaseModel):
    """
    Lightweight entity representation surfaced in the evidence trace.
    display_name is the original source name — canonical_name is never exposed.
    resolution_status='ambiguous' signals the UI to render with muted styling.
    """
    entity_id:             str
    entity_type:           str
    display_name:          str
    source_system:         str
    resolution_confidence: float
    resolution_status:     str  # 'resolved' | 'ambiguous'


class CausalHypothesisSummary(BaseModel):
    """Causal-chain hypothesis surfaced in the evidence trace (ENT-6 / T3-S16-A).

    Read live from the causal_hypotheses table (most-recent row per opportunity),
    not from run-scoped KV — like RelationshipSummary, the underlying data is
    cross-run state. All six fields are always serialised; none is excluded even
    when preliminary_reason is None.

    preliminary / preliminary_reason drive the T9 amber 'analyst review required'
    banner — preliminary_reason is null only when all three quality gates passed.
    Advisory only: this never carries or affects scoring fields.
    """
    cause_chain:              List[str]
    falsifiability_condition: str
    confidence:               float
    inferred:                 bool
    preliminary:              bool
    preliminary_reason:       Optional[str] = None


class OppEnrichment(BaseModel):
    """
    Full enrichment shape for a single opportunity.
    All list fields are always present — empty lists when LLM not available.
    This consistent shape prevents UI defensive-coding against missing fields.
    """
    oppId:                str
    aiSummary:            str = ""
    aiWhyBullets:         List[str] = Field(default_factory=list)
    aiRisks:              List[str] = Field(default_factory=list)
    aiSuggestedNextSteps: List[str] = Field(default_factory=list)
    llmGenerated:         bool = False
    llmModel:             Optional[str] = None
    # Track 3 Stage 1 — T3-S11-A temporal fields
    baseline_context:     Optional[str] = None
    trend_direction:      Optional[str] = None
    anomaly_score:        Optional[float] = None
    is_anomalous:         bool = False
    first_deviation:      bool = False
    baseline_mean:        Optional[float] = None
    baseline_stddev:      Optional[float] = None
    baseline_window_days: Optional[int] = None
    run_count:            Optional[int] = None
    current_value:        Optional[float] = None
    recent_values:        List[float] = Field(default_factory=list)
    signal_key:           Optional[str] = None
    pack_id:              Optional[str] = None
    # Track 3 Stage 2 — T3-S12-A entity summaries
    # default_factory=list ensures backward compat for code that constructs
    # OppEnrichment without an entity list (existing tests, fallback paths).
    entities:             List[EntitySummary] = Field(default_factory=list)
    # Entity display threshold shared with the backend filter. The frontend uses
    # this API value so it does not duplicate ENTITY_MIN_RUN_COUNT locally.
    entity_min_run_count:  int = ENTITY_MIN_RUN_COUNT
    # Track 3 Stage 2 — T3-S13-A relationship edges.
    # Flag-gated (INFERRED_RELATIONSHIPS_ENABLED): observed-only by default,
    # observed + inferred when the flag is on. default_factory=list keeps the
    # field present (empty) for fallback paths and runs without a graph.
    relationships:        List[RelationshipSummary] = Field(default_factory=list)
    # ENT-6 / T3-S16-A — causal chain hypothesis (Section 5). Loaded live from
    # the causal_hypotheses table (most-recent row for this opportunity), not
    # run-scoped KV. Defaults to None — absence is semantically distinct from an
    # empty hypothesis, and the frontend branches on it: None -> omit the
    # section; preliminary=True -> amber banner; preliminary=False -> full
    # confirmed rendering. (Distinct from the opportunity-level preliminary
    # fields below, which are the ENT-3 enrichment gate, not the causal gates.)
    causal_hypothesis:    Optional[CausalHypothesisSummary] = None
    # ENT-2 — Cross-System Confidence Elevation corroboration fields.
    # Always present with safe defaults so the frontend needs no defensive
    # checks. Populated from the corroboration engine output stored on the
    # opportunity. Empty/False for single-source findings (no badge rendered).
    # NOTE: corroboration_label below is also the field ENT-3 references as
    # "carried through from ENT-2" — declared once here, shared by both.
    corroboration_sources:  List[str] = Field(default_factory=list)
    corroboration_label:    Optional[str] = None
    triple_corroboration:   bool = False
    corroboration_rule_ids: List[str] = Field(default_factory=list)
    # ENT-3 / T3-S15-A — LLM enrichment enterprise hardening (Section 5).
    # Graph grounding (from the T1 prompt builder against the ENT-4 graph):
    llm_grounded:             bool = False
    graph_entity_count:       int = 0          # total entities in the run's graph
    graph_entity_count_shown: int = 0          # entities shown to the model (<= 15)
    graph_truncated:          bool = False      # True when the graph was capped
    # Hallucination guard outcomes (from the T3 pipeline integration):
    hallucination_removals:   List[str] = Field(default_factory=list)  # drop reason codes
    hallucination_rewrites:   int = 0           # rule-based rewrites
    hallucination_llm_rewrites: int = 0         # second-pass LLM rewrites
    # Preliminary quality gate (from T4). Default True = analyst review required
    # until all three gates pass.
    preliminary:              bool = True
    preliminary_reason:       Optional[str] = None
    # 2.0-A1 T1 — intervention projection. Read from the STORED opportunity (the
    # run pipeline computes and persists it), never recomputed here, so the
    # projection an analyst reads is byte-identical to the one 2.0-A2 will later
    # compare a measured outcome against.
    #
    # Defaults to None — absence is semantically distinct from an empty
    # projection, and the UI branches on it: None -> omit the panel (the
    # detector has no signal profile, or the finding carries too few measured
    # instances to project); present -> render direction + band + horizon.
    # Deliberately a free-form dict rather than a nested model: the projection is
    # produced whole by discovery/projection and stored as-is, so declaring its
    # shape twice would just create a drift surface.
    projection:               Optional[Dict[str, Any]] = None


class RunEnrichment(BaseModel):
    runId:                  str
    executiveSummary:       str = ""
    opportunitiesEnriched:  int = 0
    opportunitiesFailed:    int = 0
    generatedAt:            Optional[str] = None
    llmModel:               Optional[str] = None
    available:              bool = False


class EvidencePointerSummary(BaseModel):
    """R16-B1 (T6) — one provenance pointer in an opportunity's source trail.

    Mirrors the EvidencePointer spine (T1): the mandatory spine is always
    populated; origin records observed-vs-inferred so inferred content is never
    mistaken for ground truth. The extensible retrieval fields (chunk_id,
    retrieval_result_id) are present and null in 1.6 (AC8) — retrieval (1.8)
    fills them with no schema change.
    """
    source_system:        str
    source_artifact:      str
    source_timestamp:     str
    origin:               str = "observed"
    extraction_job_id:    Optional[str] = None
    # extensible detail — null in 1.6 (AC8)
    chunk_id:             Optional[str] = None
    retrieval_result_id:  Optional[str] = None
    detector_evidence_id: Optional[str] = None
    confidence:           Optional[float] = None


class EvidenceTrace(BaseModel):
    """The source trail for one opportunity — what the full evidence trace
    (1.9) will render. ``available`` is False (never 404) when no pointers were
    stored for the opportunity, mirroring the enrichment route's contract."""
    runId:     str
    oppId:     str
    pointers:  List[EvidencePointerSummary] = Field(default_factory=list)
    available: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_run(run_id: str) -> Dict[str, Any]:
    run = db.run_get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float_list(value: Any) -> List[float]:
    if not isinstance(value, list):
        return []
    result: List[float] = []
    for item in value:
        number = _safe_float(item)
        if number is not None:
            result.append(number)
    return result


def _pack_id_for_run(run: Dict[str, Any]) -> Optional[str]:
    inputs = run.get("inputs") or {}
    input_pack_id = inputs.get("packId") if isinstance(inputs, dict) else None
    return input_pack_id or run.get("packId") or run.get("pack_id")


def _org_candidates_for_run(run: Dict[str, Any]) -> List[str]:
    inputs = run.get("inputs") or {}
    candidates: List[Optional[str]] = [
        run.get("orgId"),
        run.get("org_id"),
        inputs.get("orgId") if isinstance(inputs, dict) else None,
        inputs.get("org_id") if isinstance(inputs, dict) else None,
        "demo-org",
        "default",
    ]
    result: List[str] = []
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _run_org_id(run: Dict[str, Any]) -> Optional[str]:
    """Return the explicit org on a run payload, if present."""
    inputs = run.get("inputs") or {}
    candidates: List[Optional[str]] = [
        run.get("org_id"),
        run.get("orgId"),
        inputs.get("org_id") if isinstance(inputs, dict) else None,
        inputs.get("orgId") if isinstance(inputs, dict) else None,
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def _find_stored_opp(run_id: str, opp_id: str) -> Optional[Dict[str, Any]]:
    opps = db.run_kv_get("opps", run_id, []) or []
    return next((o for o in opps if isinstance(o, dict) and o.get("id") == opp_id), None)


def _temporal_payload(
    run: Dict[str, Any],
    run_id: str,
    opp_id: str,
    opp_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    opp_data = opp_data or {}
    stored_opp = _find_stored_opp(run_id, opp_id) or {}
    debug = stored_opp.get("_debug", {}) if isinstance(stored_opp, dict) else {}
    if not isinstance(debug, dict):
        debug = {}

    pack_id = opp_data.get("pack_id") or stored_opp.get("packId") or _pack_id_for_run(run)
    detector_id = debug.get("detector_id")
    current_value = _safe_float(
        opp_data.get("current_value")
        if opp_data.get("current_value") is not None
        else stored_opp.get("metric_value", debug.get("metric_value"))
    )
    signal_key = opp_data.get("signal_key")
    if not signal_key and pack_id and detector_id:
        signal_key = f"{pack_id}::{detector_id}::metric_value"

    recent_values = _safe_float_list(opp_data.get("recent_values"))
    baseline_mean = _safe_float(opp_data.get("baseline_mean"))
    baseline_stddev = _safe_float(opp_data.get("baseline_stddev"))
    baseline_window_days = _safe_int(opp_data.get("baseline_window_days"))
    run_count = _safe_int(opp_data.get("run_count"))

    if detector_id:
        for org_id in _org_candidates_for_run(run):
            if signal_key and not recent_values:
                try:
                    rows = get_signal_history(org_id, detector_id, signal_key, limit=5)
                    recent_values = [
                        value
                        for value in (
                            _safe_float(row.get("metric_value", row.get("value")))
                            for row in reversed(rows)
                            if isinstance(row, dict)
                        )
                        if value is not None
                    ]
                except Exception:
                    recent_values = []

            if (
                baseline_mean is None
                or baseline_stddev is None
                or baseline_window_days is None
                or run_count is None
            ):
                try:
                    baseline = get_baseline(org_id, detector_id)
                except Exception:
                    baseline = None
                if isinstance(baseline, dict):
                    baseline_mean = baseline_mean if baseline_mean is not None else _safe_float(baseline.get("baseline_mean"))
                    baseline_stddev = baseline_stddev if baseline_stddev is not None else _safe_float(baseline.get("baseline_stddev"))
                    baseline_window_days = baseline_window_days if baseline_window_days is not None else _safe_int(baseline.get("baseline_window_days"))
                    run_count = run_count if run_count is not None else _safe_int(baseline.get("run_count"))

            if recent_values or baseline_window_days is not None:
                break

    if current_value is not None and not recent_values:
        recent_values = [current_value]

    return {
        "baseline_mean": baseline_mean,
        "baseline_stddev": baseline_stddev,
        "baseline_window_days": baseline_window_days,
        "run_count": run_count,
        "current_value": current_value,
        "recent_values": recent_values,
        "signal_key": signal_key,
        "pack_id": pack_id,
    }


def _corroboration_fields(opp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract ENT-2 corroboration fields from a stored opportunity record.

    Returns safe defaults (empty lists / False / None) when the opportunity is
    missing or predates corroboration — preserving backward compatibility for
    older runs whose opps have no corroboration fields.
    """
    opp = opp or {}
    sources = opp.get("corroboration_sources") or []
    rule_ids = opp.get("corroboration_rule_ids") or []
    return {
        "corroboration_sources": list(sources) if isinstance(sources, (list, tuple)) else [],
        "corroboration_label": opp.get("corroboration_label"),
        "triple_corroboration": bool(opp.get("triple_corroboration", False)),
        "corroboration_rule_ids": list(rule_ids) if isinstance(rule_ids, (list, tuple)) else [],
    }


def _projection_field(opp: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Extract the 2.0-A1 stored projection from a stored opportunity record.

    Returns None when the opportunity is missing, predates projections, or was
    not projectable — absence is meaningful and the UI branches on it. Never
    recomputes: a served projection must be the stored one.
    """
    projection = (opp or {}).get("projection")
    return dict(projection) if isinstance(projection, dict) else None


def _full_fallback(
    opp_id: str,
    ai_rationale: str,
    entities: Optional[List[EntitySummary]] = None,
    temporal: Optional[Dict[str, Any]] = None,
    relationships: Optional[List[RelationshipSummary]] = None,
    corroboration: Optional[Dict[str, Any]] = None,
    causal_hypothesis: Optional[CausalHypothesisSummary] = None,
    projection: Optional[Dict[str, Any]] = None,
) -> OppEnrichment:
    """
    Fix 6: Return the full OppEnrichment shape on fallback.
    All list fields are empty lists — consistent with LLM-generated shape.

    ENT-2: corroboration fields are populated from the stored opportunity when
    available, else safe defaults (empty / False).
    """
    temporal = temporal or {}
    corr = corroboration or _corroboration_fields(None)
    return OppEnrichment(
        oppId=opp_id,
        aiSummary=ai_rationale,
        aiWhyBullets=[],
        aiRisks=[],
        aiSuggestedNextSteps=[],
        llmGenerated=False,
        llmModel=None,
        baseline_mean=temporal.get("baseline_mean"),
        baseline_stddev=temporal.get("baseline_stddev"),
        baseline_window_days=temporal.get("baseline_window_days"),
        run_count=temporal.get("run_count"),
        current_value=temporal.get("current_value"),
        recent_values=temporal.get("recent_values", []),
        signal_key=temporal.get("signal_key"),
        pack_id=temporal.get("pack_id"),
        entities=entities or [],
        relationships=relationships or [],
        causal_hypothesis=causal_hypothesis,
        corroboration_sources=corr["corroboration_sources"],
        corroboration_label=corr["corroboration_label"],
        triple_corroboration=corr["triple_corroboration"],
        corroboration_rule_ids=corr["corroboration_rule_ids"],
        projection=projection,
    )


def _load_entity_summaries(run_id: str) -> List[EntitySummary]:
    """Load unique entity summaries and apply the service-account filter.

    The KV store holds entities pre-populated by entity_extractor after each run.
    Service-account filter (run_count < ENTITY_MIN_RUN_COUNT) is applied here
    per Section 8 spec. Low-count entities remain in the DB for graph
    completeness but are hidden from the evidence trace. Deduplication also
    protects legacy run payloads.
    """
    raw: List[Dict[str, Any]] = db.run_kv_get("entities", run_id, []) or []
    unique_raw: Dict[str, Dict[str, Any]] = {}
    for entity in raw:
        if not isinstance(entity, dict):
            continue
        entity_id = entity.get("entity_id")
        if not entity_id:
            continue
        # Legacy payloads stored repeated occurrences in source order. The last
        # occurrence has the latest run_count and confidence snapshot.
        unique_raw[str(entity_id)] = entity

    summaries: List[EntitySummary] = []
    for e in unique_raw.values():
        if e.get("run_count", 0) < ENTITY_MIN_RUN_COUNT:
            continue
        try:
            summaries.append(EntitySummary(
                entity_id=e["entity_id"],
                entity_type=e["entity_type"],
                display_name=e["display_name"],
                source_system=e["source_system"],
                resolution_confidence=float(e["resolution_confidence"]),
                resolution_status=e["resolution_status"],
            ))
        except (KeyError, ValueError, TypeError):
            pass  # malformed entry — skip silently
    return summaries


def _load_relationship_summaries(
    run_id: str,
    opportunity: Optional[Dict[str, Any]] = None,
) -> List[RelationshipSummary]:
    """Load relationship edges for the run's graph, flag-gated (T3-S13-A T5).

    select_relationships() applies INFERRED_RELATIONSHIPS_ENABLED at population
    time: observed-only by default, observed + inferred when the flag is on.
    org_id is taken from the request tenancy context so queries stay org-scoped.

    Architecture note: relationships are intentionally loaded live from the
    queryable entity_relationships table instead of from a run-scoped KV
    artifact. The graph is cross-run state, so future upserts can affect what
    a historical run's relationship view returns.

    Never raises — relationship surfacing is advisory and must not break the
    enrichment response. Returns an empty list when the graph is empty, the
    org context is missing, or any query error occurs.
    """
    try:
        from app.middleware.tenancy import get_current_org_id
        org_id = get_current_org_id()
    except Exception as exc:
        logger.debug("relationship load skipped — org context unavailable: %s", exc)
        return []
    try:
        run = db.get_run(run_id)
    except Exception as exc:
        logger.debug("relationship load skipped — run lookup failed for %s: %s", run_id, exc)
        return []
    if run is None:
        return []
    run_org_id = _run_org_id(run)
    if run_org_id != org_id:
        logger.debug(
            "relationship load skipped — run %s belongs to org %s, request org %s",
            run_id,
            run_org_id,
            org_id,
        )
        return []
    try:
        detector_id = (opportunity or {}).get("detector_id")
        return select_relationships_for_opportunity(org_id, run_id, detector_id)
    except Exception as exc:
        logger.debug("relationship load failed for run %s: %s", run_id, exc)
        return []


def _load_evidence_pointers(run_id: str, opp_id: str) -> List[EvidencePointerSummary]:
    """Load the org-scoped evidence-pointer trail for one opportunity (T6 / AC7).

    Tenant boundary is load-bearing here: provenance points back to real
    business systems, so a run that belongs to a different org than the request
    must yield NO pointers — the same guard _load_relationship_summaries uses.
    Reads from the run-scoped pointer index written at materialization.

    Never raises — the trace is advisory; returns [] when the org context is
    missing, the run belongs to another org, or any lookup error occurs.
    """
    try:
        from app.middleware.tenancy import get_current_org_id
        org_id = get_current_org_id()
    except Exception as exc:
        logger.debug("evidence trace skipped — org context unavailable: %s", exc)
        return []
    try:
        run = db.get_run(run_id)
    except Exception as exc:
        logger.debug("evidence trace skipped — run lookup failed for %s: %s", run_id, exc)
        return []
    if run is None:
        return []
    run_org_id = _run_org_id(run)
    if run_org_id is not None and run_org_id != org_id:
        logger.debug(
            "evidence trace skipped — run %s belongs to org %s, request org %s",
            run_id, run_org_id, org_id,
        )
        return []
    try:
        from .evidence_pointers import get_evidence_pointers_for_opportunity
        raw = get_evidence_pointers_for_opportunity(run_id, opp_id)
    except Exception as exc:
        logger.debug("evidence trace load failed for run %s opp %s: %s", run_id, opp_id, exc)
        return []

    summaries: List[EvidencePointerSummary] = []
    for p in raw:
        try:
            summaries.append(EvidencePointerSummary(
                source_system=p["source_system"],
                source_artifact=p["source_artifact"],
                source_timestamp=p["source_timestamp"],
                origin=p.get("origin", "observed"),
                extraction_job_id=p.get("extraction_job_id"),
                chunk_id=p.get("chunk_id"),
                retrieval_result_id=p.get("retrieval_result_id"),
                detector_evidence_id=p.get("detector_evidence_id"),
                confidence=p.get("confidence"),
            ))
        except (KeyError, TypeError, ValueError):
            pass  # malformed pointer — skip silently
    return summaries


def _load_causal_hypothesis(
    org_id: Optional[str], opportunity_id: str, run_id: str
) -> Optional[CausalHypothesisSummary]:
    """Load the causal hypothesis for the exact org, run, and opportunity.

    Read live from the causal_hypotheses table (ENT-6 / T3-S16-A), not from a
    run-scoped KV artifact. Opportunity IDs can repeat across runs, so run_id
    is required to prevent historical hypotheses from leaking into the current
    Opportunity Review. Scoped to org_id, so a different org's hypothesis can
    never be returned.

    Advisory and never raises: returns None when there is no hypothesis, when
    the org context is missing, or on any query error. Absence is the normal
    state, so it is deliberately NOT logged as a warning. Touches no scoring
    fields (impact, effort, tier, decision, evidence ids).
    """
    if not org_id or not opportunity_id or not run_id:
        return None
    try:
        conn = db.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT cause_chain, falsifiability_condition, confidence,
                       inferred, preliminary, preliminary_reason
                FROM causal_hypotheses
                WHERE org_id = %s AND opportunity_id = %s AND run_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (org_id, opportunity_id, run_id),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("causal hypothesis load failed for opp %s: %s", opportunity_id, exc)
        return None

    if row is None:
        return None  # absence is the normal state — no warning

    try:
        cause_chain = json.loads(row["cause_chain"]) if row["cause_chain"] else []
    except (TypeError, ValueError):
        cause_chain = []

    return CausalHypothesisSummary(
        cause_chain=cause_chain,
        falsifiability_condition=row["falsifiability_condition"] or "",
        confidence=float(row["confidence"]),
        inferred=bool(row["inferred"]),
        preliminary=bool(row["preliminary"]),
        preliminary_reason=row["preliminary_reason"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────────

def register_sprint4_t6_routes(app) -> None:

    @app.get(
        "/api/runs/{run_id}/opportunities/{opp_id}/enrichment",
        response_model=OppEnrichment,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        tags=["runs"],
    )
    def get_opp_enrichment(run_id: str, opp_id: str) -> OppEnrichment:
        """
        Get LLM enrichment for a single opportunity.

        Always returns a usable OppEnrichment object:
        - If enrichment exists: returns LLM-generated fields
        - If enrichment missing: returns aiRationale as aiSummary, empty lists
        - Never returns 404 for missing enrichment (only for unknown runId/oppId)
        - entities field is always present — populated from run KV when available
        """
        run = _require_run(run_id)

        # Load entity summaries once for this run (shared across all opportunities).
        # Service-account filter is applied here per spec Section 8.
        entity_summaries = _load_entity_summaries(run_id)

        enrichment = db.run_kv_get(KV_LLM_ENRICHMENT, run_id, None)

        # R18-C1 T4: resolve the run's active-template terminology once and adapt
        # the finding-detail wording (aiSummary/why/risks/next-steps, the
        # aiRationale fallback, corroboration label) to the domain language.
        # No-op when no template is active — technical fields are untouched.
        terminology = resolve_run_terminology(run_id)

        # ENT-2: corroboration fields live on the stored opportunity record.
        # Load the opps list once so both branches can read them.
        stored_opps = db.run_kv_get("opps", run_id, []) or []
        stored_opp = next((o for o in stored_opps if o.get("id") == opp_id), None)
        stored_opp = apply_terminology(stored_opp, terminology)
        corroboration = _corroboration_fields(stored_opp)
        # 2.0-A1 T1: the intervention projection also lives on the stored
        # opportunity (written by the run pipeline). Served as stored.
        projection = _projection_field(stored_opp)

        relationship_summaries = _load_relationship_summaries(run_id, stored_opp)

        try:
            from app.middleware.tenancy import get_current_org_id_optional
            causal_org_id = get_current_org_id_optional()
        except Exception:
            causal_org_id = None
        causal_hypothesis = _load_causal_hypothesis(causal_org_id, opp_id, run_id)

        # Enrichment not yet generated — serve fallback from stored opps
        if enrichment is None:
            if stored_opp is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Opportunity '{opp_id}' not found in run '{run_id}'"
                )
            temporal = _temporal_payload(run, run_id, opp_id)
            return _full_fallback(
                opp_id,
                stored_opp.get("aiRationale", ""),
                entity_summaries,
                temporal,
                relationship_summaries,
                corroboration,
                causal_hypothesis,
                projection,
            )

        per_opp  = enrichment.get("perOpportunity", {})
        opp_data = per_opp.get(opp_id)

        if opp_data is None:
            raise HTTPException(
                status_code=404,
                detail=f"Opportunity '{opp_id}' not found in enrichment for run '{run_id}'"
            )

        # R18-C1 T4: adapt the LLM narrative fields to the active template's
        # domain language before serving. Numeric/graph/temporal fields on
        # opp_data are outside the terminology allowlist and stay verbatim.
        opp_data = apply_terminology(opp_data, terminology)

        temporal = _temporal_payload(run, run_id, opp_id, opp_data)

        return OppEnrichment(
            oppId=opp_id,
            aiSummary=opp_data.get("aiSummary", ""),
            aiWhyBullets=opp_data.get("aiWhyBullets", []),
            aiRisks=opp_data.get("aiRisks", []),
            aiSuggestedNextSteps=opp_data.get("aiSuggestedNextSteps", []),
            llmGenerated=opp_data.get("llmGenerated", False),
            llmModel=opp_data.get("llmModel"),
            baseline_context=opp_data.get("baseline_context"),
            trend_direction=opp_data.get("trend_direction"),
            anomaly_score=opp_data.get("anomaly_score"),
            is_anomalous=opp_data.get("is_anomalous", False),
            first_deviation=opp_data.get("first_deviation", False),
            baseline_mean=temporal.get("baseline_mean"),
            baseline_stddev=temporal.get("baseline_stddev"),
            baseline_window_days=temporal.get("baseline_window_days"),
            run_count=temporal.get("run_count"),
            current_value=temporal.get("current_value"),
            recent_values=temporal.get("recent_values", []),
            signal_key=temporal.get("signal_key"),
            pack_id=temporal.get("pack_id"),
            entities=entity_summaries,
            relationships=relationship_summaries,
            causal_hypothesis=causal_hypothesis,
            # ENT-2 — Cross-System Confidence Elevation (from the stored opp).
            # corroboration_label here is the single source; ENT-3 reads the
            # same field, so it is not set a second time below.
            corroboration_sources=corroboration["corroboration_sources"],
            corroboration_label=corroboration["corroboration_label"],
            triple_corroboration=corroboration["triple_corroboration"],
            corroboration_rule_ids=corroboration["corroboration_rule_ids"],
            # ENT-3 / T3-S15-A — graph grounding, guard outcomes, quality gate.
            llm_grounded=opp_data.get("llm_grounded", False),
            graph_entity_count=opp_data.get("graph_entity_count", 0),
            graph_entity_count_shown=opp_data.get("graph_entity_count_shown", 0),
            graph_truncated=opp_data.get("graph_truncated", False),
            hallucination_removals=opp_data.get("hallucination_removals", []) or [],
            hallucination_rewrites=opp_data.get("hallucination_rewrites", 0),
            hallucination_llm_rewrites=opp_data.get("hallucination_llm_rewrites", 0),
            preliminary=opp_data.get("preliminary", True),
            preliminary_reason=opp_data.get("preliminary_reason"),
            # 2.0-A1 T1 — intervention projection, from the stored opportunity.
            projection=projection,
        )

    @app.get(
        "/api/runs/{run_id}/llm-enrichment",
        response_model=RunEnrichment,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        tags=["runs"],
    )
    def get_run_enrichment(run_id: str) -> RunEnrichment:
        """
        Get LLM enrichment status and executive summary for a run.
        Returns available: false if enrichment not yet generated — not 404.
        """
        _require_run(run_id)
        enrichment = db.run_kv_get(KV_LLM_ENRICHMENT, run_id, None)

        if enrichment is None:
            return RunEnrichment(runId=run_id, available=False)

        # R18-C1 T4: adapt the run-level executive summary to the active
        # template's domain language (this is the executive-reporting surface).
        executive_summary = apply_terminology(
            {"executiveSummary": enrichment.get("executiveSummary", "")},
            resolve_run_terminology(run_id),
        )["executiveSummary"]

        return RunEnrichment(
            runId=run_id,
            executiveSummary=executive_summary,
            opportunitiesEnriched=enrichment.get("opportunitiesEnriched", 0),
            opportunitiesFailed=enrichment.get("opportunitiesFailed", 0),
            generatedAt=enrichment.get("generatedAt"),
            llmModel=enrichment.get("llmModel"),
            available=True,
        )

    @app.get(
        "/api/runs/{run_id}/opportunities/{opp_id}/evidence-trace",
        response_model=EvidenceTrace,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        tags=["runs"],
    )
    def get_evidence_trace(run_id: str, opp_id: str) -> EvidenceTrace:
        """R16-B1 (T6 / AC7) — walk an opportunity back to its source artifacts.

        Returns the provenance trail (source_system + source_artifact +
        source_timestamp per pointer) the full evidence trace will render in 1.9.
        The backend can answer 'where did this finding come from' even though the
        frontend may not display the trail yet.

        Org-scoped: a run belonging to another org yields an empty (available:
        false) trail, never another tenant's provenance. Returns 404 only for an
        unknown run or an opportunity absent from the run — mirroring the
        enrichment route's contract (never 404 for a merely empty trail).
        """
        _require_run(run_id)

        stored_opp = _find_stored_opp(run_id, opp_id)
        if stored_opp is None:
            raise HTTPException(
                status_code=404,
                detail=f"Opportunity '{opp_id}' not found in run '{run_id}'",
            )

        pointers = _load_evidence_pointers(run_id, opp_id)
        return EvidenceTrace(
            runId=run_id,
            oppId=opp_id,
            pointers=pointers,
            available=bool(pointers),
        )
