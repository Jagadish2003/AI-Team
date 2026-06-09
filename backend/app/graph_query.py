"""Knowledge-graph read layer for Stage 2 (T3-S13-A).

This module is the single read path for relationship edges that flow into API
responses. It is deliberately separate from relationship_mapper.py (the write
path): mapping decides what is stored, querying decides what is surfaced.

T5 deliverables:
  get_observed_relationships(org_id, run_id) — observed edges only (inferred=0).
  get_all_relationships(org_id, run_id)      — observed + inferred edges.
  select_relationships(org_id, run_id)       — flag-aware selector used by the
                                               OppEnrichment population step.

All queries are scoped to org_id (cross-org isolation) and to the run via
last_seen_run_id — the run pipeline re-confirms every current edge each run, so
last_seen_run_id = run_id identifies the edges belonging to that run's graph.

The flag check lives in select_relationships(), i.e. at population time — never
at query time. Inferred edges are always stored; the flag only decides what is
returned. get_observed_relationships()/get_all_relationships() are unconditional
building blocks so callers (and Stage 3 analysis) can always reach either set.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import List

from pydantic import BaseModel

from app import db
from app.config import inferred_relationships_enabled

logger = logging.getLogger(__name__)


class RelationshipSummary(BaseModel):
    """Edge representation surfaced in OppEnrichment.relationships (T7 field).

    inferred drives the UI '[inferred]' label and is load-bearing for T3-S15-A
    LLM prompt construction — it must always reflect the stored edge's value.
    Field names match the dataclass specified in Section 5 of the story.
    """
    from_entity_name: str
    from_entity_type: str
    relationship_type: str
    to_entity_name: str
    to_entity_type: str
    inferred: bool
    confidence: float


# Edge row joined to both endpoint entities for display names/types. INNER JOIN:
# an edge whose endpoints are not resolvable to entities cannot produce a
# summary and is omitted. Ordered deterministically for stable responses.
_SELECT_EDGES = """
    SELECT
        er.relationship_type AS relationship_type,
        er.inferred          AS inferred,
        er.confidence        AS confidence,
        ef.display_name      AS from_entity_name,
        ef.entity_type       AS from_entity_type,
        et.display_name      AS to_entity_name,
        et.entity_type       AS to_entity_type
    FROM entity_relationships er
    JOIN entities ef ON ef.id = er.from_entity_id AND ef.org_id = er.org_id
    JOIN entities et ON et.id = er.to_entity_id   AND et.org_id = er.org_id
    WHERE er.org_id = ?
      AND er.last_seen_run_id = ?
      {inferred_filter}
    ORDER BY er.inferred ASC, er.relationship_type ASC,
             ef.display_name ASC, et.display_name ASC
"""


def _query(org_id: str, run_id: str, observed_only: bool) -> List[RelationshipSummary]:
    """Run the edge query for a run, optionally filtering to observed edges."""
    inferred_filter = "AND er.inferred = 0" if observed_only else ""
    sql = _SELECT_EDGES.format(inferred_filter=inferred_filter)

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (org_id, run_id)).fetchall()
    finally:
        conn.close()

    summaries: List[RelationshipSummary] = []
    for row in rows:
        summaries.append(
            RelationshipSummary(
                from_entity_name=row["from_entity_name"],
                from_entity_type=row["from_entity_type"],
                relationship_type=row["relationship_type"],
                to_entity_name=row["to_entity_name"],
                to_entity_type=row["to_entity_type"],
                inferred=bool(row["inferred"]),
                confidence=float(row["confidence"]),
            )
        )
    return summaries


def get_observed_relationships(org_id: str, run_id: str) -> List[RelationshipSummary]:
    """Return only directly observed edges (inferred=False) for a run.

    These are graph truth — owns / member_of / escalates_to derived from
    explicit source fields. Scoped to org_id and run.
    """
    return _query(org_id, run_id, observed_only=True)


def get_all_relationships(org_id: str, run_id: str) -> List[RelationshipSummary]:
    """Return all edges — observed and inferred — for a run.

    The inferred=True flag is preserved on each summary so the UI can render
    the '[inferred]' label. Scoped to org_id and run.
    """
    return _query(org_id, run_id, observed_only=False)


def select_relationships(org_id: str, run_id: str) -> List[RelationshipSummary]:
    """Flag-aware selector used by the OppEnrichment population step.

    INFERRED_RELATIONSHIPS_ENABLED=False (default) → observed edges only.
    INFERRED_RELATIONSHIPS_ENABLED=True            → observed + inferred edges.

    The flag is evaluated here, at population time. Inferred edges are always
    stored regardless of the flag — this only decides what is returned.
    """
    if inferred_relationships_enabled():
        return get_all_relationships(org_id, run_id)
    return get_observed_relationships(org_id, run_id)
