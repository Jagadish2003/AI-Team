"""Knowledge-graph read layer for Stage 2 (T3-S13-A).

This module is the single read path for relationship edges that flow into API
responses. It is deliberately separate from relationship_mapper.py (the write
path): mapping decides what is stored, querying decides what is surfaced.

T5 deliverables:
  get_observed_relationships(org_id, run_id) — observed edges only (inferred=0).
  get_all_relationships(org_id, run_id)      — observed + inferred edges.
  select_relationships(org_id, run_id)       — flag-aware selector used by the
                                               OppEnrichment population step.

T8 deliverable:
  get_entity_relationships(org_id, entity_id, inferred=False)
    — single-hop entity-scoped query: all edges touching a specific entity.
      inferred=False (default) returns only directly observed edges.
      inferred=True returns all edges (observed + inferred).
      The default is architecturally intentional: T3-S14-A's recursive CTE
      graph queries call this with the default so the traversal is built on
      solid observed foundations. T3-S15-A passes inferred=True only when
      INFERRED_RELATIONSHIPS_ENABLED is active. Any caller that omits the
      parameter safely gets observed edges only.

T5 queries are scoped to org_id (cross-org isolation) and to the queried run's
chronological position. A relationship remains visible for every run at or
after first_seen_run_id, even when a later upsert moves last_seen_run_id
forward.

T8 query is entity-scoped, not run-scoped: it returns edges across all runs
that involve the specified entity. This is intentional — graph traversal must
see the full history of confirmed edges, not just the current run window.

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


# Edge row joined to both endpoint entities for display names/types, and to
# runs for chronological visibility and run/org ownership. INNER JOIN:
# an edge whose endpoints or first-seen run are not resolvable cannot produce a
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
    JOIN runs queried_run ON queried_run.id = ?
    JOIN runs first_seen_run ON first_seen_run.id = er.first_seen_run_id
    JOIN entities ef ON ef.id = er.from_entity_id AND ef.org_id = er.org_id
    JOIN entities et ON et.id = er.to_entity_id   AND et.org_id = er.org_id
    WHERE er.org_id = ?
      AND COALESCE(
            json_extract(queried_run.payload, '$.org_id'),
            json_extract(queried_run.payload, '$.orgId')
          ) = er.org_id
      AND COALESCE(
            json_extract(first_seen_run.payload, '$.org_id'),
            json_extract(first_seen_run.payload, '$.orgId')
          ) = er.org_id
      AND first_seen_run.rowid <= queried_run.rowid
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
        rows = conn.execute(sql, (run_id, org_id)).fetchall()
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


# Entity-scoped SQL: returns all edges where the entity appears as either
# endpoint, across all runs. Separate template from _SELECT_EDGES because:
# (a) filter is on entity_id + org_id rather than run chronology, and
# (b) ORDER BY is the same deterministic ordering so callers get stable results.
# TODO(T3-S14): add pagination before this is exposed as a broad public API.
# Current Sprint 13 callers use internal, single-hop lookups with small
# row-count ceilings: a handful of edges per entity in demo/offline packs.
_SELECT_ENTITY_EDGES = """
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
      AND (er.from_entity_id = ? OR er.to_entity_id = ?)
      {inferred_filter}
    ORDER BY er.inferred ASC, er.relationship_type ASC,
             ef.display_name ASC, et.display_name ASC
"""


def get_entity_relationships(
    org_id: str,
    entity_id: str,
    inferred: bool = False,
) -> List[RelationshipSummary]:
    """Return all edges for a specific entity (single-hop lookup).

    Queries edges where from_entity_id=entity_id OR to_entity_id=entity_id
    within the given org_id, across all runs.

    inferred=False (default): returns only directly observed edges (inferred=0).
      T3-S14-A recursive CTE queries call with this default — the traversal is
      built on solid observed foundations, not co-firing hypotheses.
    inferred=True: returns all edges including inferred ones.
      T3-S15-A LLM context builder passes inferred=True only when
      INFERRED_RELATIONSHIPS_ENABLED is active.

    The inferred=False default is architecturally intentional. Any caller that
    does not explicitly pass inferred=True gets observed edges only — preventing
    inferred hypotheses from accidentally entering graph traversal or LLM prompts.

    Cross-org isolation: the entity JOIN is scoped by org_id on both sides,
    so an entity_id that happens to exist in another org cannot produce a match.
    """
    inferred_filter = "" if inferred else "AND er.inferred = 0"
    sql = _SELECT_ENTITY_EDGES.format(inferred_filter=inferred_filter)

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (org_id, entity_id, entity_id)).fetchall()
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
