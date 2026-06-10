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

logger = logging.getLogger(__name__)

# ENT-4 graph cap: the prompt reasons over at most 15 entities so the context
# stays within a predictable token budget. A larger graph is truncated and the
# model is told it is seeing a partial view via the truncation note.
MAX_GRAPH_ENTITIES = 15

# Below this many entities the graph is too thin to ground against — the
# enrichment falls back to the pre-ENT-3 prompt and runs no hallucination guard.
SPARSE_GRAPH_THRESHOLD = 3


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


def _entity_get(entity: Any, key: str, default: Any = None) -> Any:
    """Read a field from an entity that may be a dict or an object."""
    if isinstance(entity, dict):
        return entity.get(key, default)
    return getattr(entity, key, default)


def _load_entities(run_id: str) -> List[Dict[str, Any]]:
    """Load this run's entity summaries from KV. Never raises."""
    try:
        raw = db.run_kv_get("entities", run_id, []) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("graph_context: entity load failed for %s: %s", run_id, exc)
        return []
    return [e for e in raw if isinstance(e, dict)]


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
        entities = _load_entities(run_id)
    if relationships is None:
        relationships = _load_relationships(org_id, run_id) if org_id else []

    # Deterministic ordering so the same graph always yields the same prompt.
    safe_entities = [e for e in (entities or []) if isinstance(e, dict)]
    safe_entities.sort(
        key=lambda e: (
            str(_entity_get(e, "display_name", "")).lower(),
            str(_entity_get(e, "entity_id", "")),
        )
    )

    total = len(safe_entities)
    truncated = total > MAX_GRAPH_ENTITIES
    shown_entities = safe_entities[:MAX_GRAPH_ENTITIES]
    shown = len(shown_entities)
    is_sparse = total < SPARSE_GRAPH_THRESHOLD

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

    observed_summary = _render_observed_summary(shown_entities, relationships or [])

    return GraphContext(
        entity_count=total,
        entity_count_shown=shown,
        truncated=truncated,
        is_sparse=is_sparse,
        resolved_names=resolved_names,
        observed_summary=observed_summary,
        truncation_note=truncation_note,
        relationship_count=len(relationships or []),
    )
