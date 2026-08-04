"""ENT-3 / T3-S15-A — Graph context builder for grounded LLM enrichment.

This module is the bridge between the ENT-4 knowledge graph (entities stored in
run KV by ``app.entity_extractor`` and relationship edges served by
``app.graph_query``) and the first-pass enrichment prompt built in
``app.llm_enrichment``.

``build_graph_context(org_id, run_id)`` assembles a :class:`GraphContext` that:

  * caps the entity set at :data:`MAX_GRAPH_ENTITIES` (15) and records whether
    the graph was truncated, plus the total and shown entity counts — these
    feed ``OppEnrichment.graph_entity_count`` / ``graph_entity_count_shown`` /
    ``graph_truncated`` (T5);
  * exposes ``resolved_names`` (lowercased display names of *resolved* entities)
    so the hallucination guard (T2/T3) can tell which proper nouns are real;
  * renders a deterministic ``observed_summary`` text block and a
    ``truncation_note`` for the DIRECTLY OBSERVED section of the prompt;
  * flags ``is_sparse`` when the graph holds fewer than
    :data:`SPARSE_GRAPH_THRESHOLD` (3) entities — the signal the enrichment
    pipeline uses to fall back to the pre-ENT-3 prompt and skip the guard
    (AC10).

The build is deterministic: entities are sorted by (display_name, entity_id)
and relationship edges arrive already deterministically ordered from
``graph_query``. Given the same stored graph, ``build_graph_context`` returns an
identical context, which is what lets the first-pass prompt be byte-stable
(AC1). The function never raises — a missing graph, missing org context, or any
query error degrades to an empty (sparse) context so enrichment continues.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app import db
from app.context_assembly import AssemblyPolicy, assemble_context
from app.graph_constants import (
    GRAPH_CONTEXT_MAX_ENTITIES as MAX_GRAPH_ENTITIES,
    SPARSE_GRAPH_THRESHOLD,
)

logger = logging.getLogger(__name__)

# ENT-4 graph cap: the prompt reasons over at most 15 entities so the context
# stays within a predictable token budget. A larger graph is truncated and the
# model is told it is seeing a partial view via the truncation note.
#
# MAX_GRAPH_ENTITIES (15) and SPARSE_GRAPH_THRESHOLD (3) are imported from
# app.graph_constants — the single source of truth shared with
# app.graph_context_builder — and re-exported under these names so existing
# importers/tests are unaffected (ENT-4 review #9).


class GraphContext(BaseModel):
    """Assembled, prompt-ready view of a run's knowledge graph.

    All fields are populated deterministically by :func:`build_graph_context`.
    ``resolved_names`` is the load-bearing input for the hallucination guard;
    the count/truncation fields are surfaced verbatim on ``OppEnrichment``.
    """

    entity_count: int = 0                 # total resolved+ambiguous entities (pre-cap)
    entity_count_shown: int = 0           # entities actually rendered (post-cap, <= 15)
    truncated: bool = False               # True when entity_count > MAX_GRAPH_ENTITIES
    is_sparse: bool = True                # True when entity_count < SPARSE_GRAPH_THRESHOLD
    resolved_names: List[str] = Field(default_factory=list)  # lowercased display names
    observed_summary: str = ""            # DIRECTLY OBSERVED section body
    truncation_note: str = ""             # appended under observed_summary when truncated
    relationship_count: int = 0           # edges included in the summary
    # R16-B2: the include/exclude decisions made by the context assembly service
    # for this run's graph context — surfaced for auditability (why each entity /
    # edge was kept or dropped). Empty when assembly logged nothing.
    selection_log: List[Dict[str, Any]] = Field(default_factory=list)


def _entity_get(entity: Any, key: str, default: Any = None) -> Any:
    """Read a field from an entity that may be a dict or an object."""
    if isinstance(entity, dict):
        return entity.get(key, default)
    return getattr(entity, key, default)


def _load_entities_from_table(org_id: str, run_id: str) -> List[Dict[str, Any]]:
    """Load the org's accumulated entity set visible as of ``run_id`` from the
    entities table. Never raises; returns [] on any error.

    This uses the shared ``ENTITIES_VISIBLE_AS_OF_RUN_FROM_WHERE`` clause — the
    same chronological rowid approach the entities API route (PR #113 fix) uses —
    so an entity first seen in an earlier run is still visible for this run's
    context. Rows expose ``entity_id`` (aliased from ``id``) to match the shape
    the rest of this module expects from KV-loaded entities.
    """
    try:
        from database.models.entities import ENTITIES_VISIBLE_AS_OF_RUN_FROM_WHERE

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT e.id AS entity_id, e.display_name, e.entity_type, "
                "e.source_system, e.resolution_status, e.resolution_confidence "
                + ENTITIES_VISIBLE_AS_OF_RUN_FROM_WHERE,
                (org_id, run_id),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "graph_context: entities-table fallback failed for %s/%s: %s",
            org_id, run_id, exc,
        )
        return []


def _load_entities(org_id: Optional[str], run_id: str) -> List[Dict[str, Any]]:
    """Load this run's entity summaries. Never raises.

    Primary source is the run-scoped KV blob written during materialization
    (deterministic, run-specific). When that blob is empty — which can happen if
    the run's entities were first seen in a *prior* run and were not re-written
    into this run's KV — we fall back to the accumulated org entity set from the
    entities table (review #5). Without this, ``resolved_names`` would be
    incomplete and the hallucination guard could wrongly flag legitimate,
    previously-seen entity names and drop valid bullets.
    """
    try:
        raw = db.run_kv_get("entities", run_id, []) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("graph_context: entity load failed for %s: %s", run_id, exc)
        raw = []
    entities = [e for e in raw if isinstance(e, dict)]
    if entities:
        return entities
    # KV had no entities for this run — recover the accumulated org set so the
    # guard sees every legitimate name visible as of this run.
    if org_id:
        return _load_entities_from_table(org_id, run_id)
    return entities


def _load_relationships(org_id: str, run_id: str) -> List[Any]:
    """Load this run's relationship edges (flag-aware). Never raises."""
    try:
        from app.graph_query import select_relationships

        return select_relationships(org_id, run_id)
    except Exception as exc:
        logger.debug("graph_context: relationship load failed for %s: %s", run_id, exc)
        return []


def _render_observed_summary(
    entities: List[Dict[str, Any]],
    relationships: List[Any],
) -> str:
    """Render the DIRECTLY OBSERVED section body deterministically.

    Lists the (capped) entities first, then the relationship edges. Inferred
    edges are explicitly labelled so the model can tag derived bullets
    [INFERRED: ...] rather than presenting them as observed fact.
    """
    lines: List[str] = []
    if entities:
        lines.append("Entities:")
        for e in entities:
            name = _entity_get(e, "display_name", "")
            etype = _entity_get(e, "entity_type", "")
            source = _entity_get(e, "source_system", "")
            status = _entity_get(e, "resolution_status", "")
            suffix = " [ambiguous]" if status == "ambiguous" else ""
            lines.append(f"- {name} ({etype}, {source}){suffix}")
    else:
        lines.append("Entities: none resolved for this finding.")

    if relationships:
        lines.append("Relationships:")
        for r in relationships:
            frm = _entity_get(r, "from_entity_name", "")
            rel = str(_entity_get(r, "relationship_type", "")).replace("_", " ")
            to = _entity_get(r, "to_entity_name", "")
            inferred = bool(_entity_get(r, "inferred", False))
            label = " [inferred]" if inferred else ""
            lines.append(f"- {frm} {rel} {to}{label}")

    return "\n".join(lines)


def build_graph_context(
    org_id: Optional[str],
    run_id: str,
    entities: Optional[List[Dict[str, Any]]] = None,
    relationships: Optional[List[Any]] = None,
) -> GraphContext:
    """Assemble the prompt-ready :class:`GraphContext` for a run.

    ``entities`` / ``relationships`` may be supplied directly (used by tests and
    to avoid re-querying); when omitted they are loaded from run KV and the
    ENT-4 graph query layer. The build is deterministic and never raises:
    on any failure it returns an empty, sparse context so the caller falls back
    to the pre-ENT-3 prompt.

    The 15-entity cap is applied here; ``entity_count`` reflects the full graph
    while ``entity_count_shown`` reflects what the prompt actually contains.
    """
    if entities is None:
        entities = _load_entities(org_id, run_id)
    if relationships is None:
        relationships = _load_relationships(org_id, run_id) if org_id else []

    safe_entities = [e for e in (entities or []) if isinstance(e, dict)]
    safe_relationships = list(relationships or [])

    # Totals are measured BEFORE selection: entity_count / is_sparse / truncated
    # describe the whole graph the run touched, not just what survived the cap.
    total = len(safe_entities)
    truncated = total > MAX_GRAPH_ENTITIES
    is_sparse = total < SPARSE_GRAPH_THRESHOLD

    # R16-B2 (T5/AC8): the final context-SELECTION policy — budget/caps,
    # deterministic ranking, observed-first, and the selection log — lives in ONE
    # place: the context assembly service. This module still PRODUCES the graph
    # (loads entities/edges and renders the observed summary), but it no longer
    # decides the selection itself. Routing the enrichment grounding context
    # through assemble_context() means enrichment, retrieval (1.8) and future
    # causal reasoning all share the same rules, so the chosen context is
    # explainable and reproducible everywhere.
    #
    # R18-B1 (T6): the evidence_source hook now carries the REAL retrieval
    # substrate — the org-scoped source built on retrieve(). Retrieval PROPOSES
    # candidate chunks; the assembler DECIDES what enters context under the same
    # floor/ordering/cap/log rules as the graph (AC6) — retrieval never feeds
    # enrichment directly. The run-level opportunity passed here carries no query
    # text yet, so the source proposes nothing today; opportunity-level callers
    # (Sprint-2 deep-content stories) get retrieved evidence through this same
    # hook with no further wiring. Import is guarded: a broken/unavailable
    # retrieval substrate degrades to the 1.6 behaviour (no evidence source),
    # never a failed graph context.
    evidence_source = None
    if org_id:
        try:
            from app.retrieval.evidence_source import retrieval_evidence_source

            evidence_source = retrieval_evidence_source(org_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("graph_context: retrieval evidence source unavailable: %s", exc)
    # 2.0-B3 T1 (AC1): the assembly policy comes from the DECLARATION
    # (app/config/assembly_policy.json), so editing precedence there changes what
    # this run composes with no code deploy. Without this the declaration would be
    # inert and AC1 would hold only in the abstract.
    #
    # Degrades rather than raises, unlike the loader's own contract: graph context
    # is a non-blocking enrichment step (a failure here must never cost a run its
    # findings), so a broken declaration falls back to the R16-B2 in-code defaults
    # and says so LOUDLY at error level — naming the consequence, because a silent
    # fallback would mean composing against precedence nobody chose.
    try:
        policy = AssemblyPolicy.declared()
    except Exception as exc:  # noqa: BLE001 — enrichment must not fail the run.
        logger.error(
            "graph_context: declared assembly policy unusable (%s) — falling back to "
            "the built-in R16-B2 precedence for run %s; source-type precedence and "
            "any configured caps/floor are NOT in effect",
            exc, run_id,
        )
        policy = AssemblyPolicy()
    package = assemble_context(
        opportunity={"run_id": run_id},
        graph={"entities": safe_entities, "relationships": safe_relationships},
        policy=policy,
        evidence_source=evidence_source,
    )
    shown_entities = package.entities          # selected, ordered, <= 15
    shown_relationships = package.relationships  # selected, ordered, <= 20
    shown = len(shown_entities)

    resolved_names = [
        str(_entity_get(e, "display_name", "")).lower()
        for e in shown_entities
        if _entity_get(e, "resolution_status") == "resolved"
        and str(_entity_get(e, "display_name", "")).strip()
    ]

    truncation_note = ""
    if truncated:
        truncation_note = (
            f"Note: this finding touches {total} entities; the {shown} most "
            f"relevant are shown above. Reason over this partial view only — do "
            f"not infer the names of entities that are not listed."
        )

    observed_summary = _render_observed_summary(shown_entities, shown_relationships)

    return GraphContext(
        entity_count=total,
        entity_count_shown=shown,
        truncated=truncated,
        is_sparse=is_sparse,
        resolved_names=resolved_names,
        observed_summary=observed_summary,
        truncation_note=truncation_note,
        relationship_count=len(shown_relationships),
        selection_log=package.selection_log,
    )
