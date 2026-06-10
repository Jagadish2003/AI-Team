"""Causal inference engine for Stage 3 (T3-S16-A / ENT-6).

This module owns:
  CausalContext - the assembled data package fed to the causal-chain prompt.
  InsufficientGraphContextError - raised when entity count < 3 (AC9).
  build_causal_context() - assembles graph neighbourhood, dependency paths,
                           and temporal support for process entities.
  is_generic_falsifiability() - flags semantically empty disproof conditions.
  parse_causal_output() - parses and validates LLM causal output.

ENT-4 dependency note
---------------------
Section 2a specifies opportunity_neighbourhood() and entity_path() from
graph_query.py as the intended graph API. Those functions are ENT-4
deliverables and are not present in the current branch - graph_query.py
today only exposes get_entity_relationships(org_id, entity_id, inferred=False).

Decision (option a): this module provides a minimal, clearly-scoped in-engine
implementation of both primitives on top of get_entity_relationships():
  _depth3_neighbourhood() - BFS to depth 3 with inferred=True.
  _shortest_path()        - bidirectional BFS between two entity IDs.

When ENT-4 merges and graph_query.py exposes opportunity_neighbourhood() /
entity_path(), replace the two private helpers with imports and delete this
note. The public signature of build_causal_context() is unchanged.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from app import db
    from app.temporal_enrichment import build_baseline_context
    from app.trend_engine import calculate_anomaly, calculate_trend
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports
    from backend.app import db  # type: ignore[no-redef]
    from backend.app.temporal_enrichment import build_baseline_context  # type: ignore[no-redef]
    from backend.app.trend_engine import calculate_anomaly, calculate_trend  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class InsufficientGraphContextError(Exception):
    """Raised when the entity neighbourhood has fewer than 3 entities.

    Callers catch this, skip hypothesis generation, and log
    causal.hypothesis_rejected with reason='insufficient_graph_context'.
    """


# ---------------------------------------------------------------------------
# Graph neighbourhood data containers
# ---------------------------------------------------------------------------

@dataclass
class EntityNode:
    """Lightweight entity representation within a causal neighbourhood."""

    entity_id: str
    entity_type: str
    display_name: str
    resolution_status: str
    org_id: str


@dataclass
class EdgeNode:
    """Edge representation within a causal neighbourhood."""

    from_entity_id: str
    to_entity_id: str
    relationship_type: str
    inferred: bool
    confidence: float


@dataclass
class GraphNeighbourhood:
    """Depth-3 neighbourhood around a set of seed entities."""

    entities: list[EntityNode] = field(default_factory=list)
    edges: list[EdgeNode] = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        return len(self.entities)


# ---------------------------------------------------------------------------
# CausalContext - the assembled data package T3 serialises into the prompt
# ---------------------------------------------------------------------------

@dataclass
class CausalContext:
    """All data needed to generate and validate a causal-chain hypothesis.

    Fields
    ------
    graph_context     : depth-3 entity/edge neighbourhood.
    dependency_paths  : pairwise shortest paths between process entities.
    temporal_support  : signal_key -> {trend, anomaly, context, run_count} for
                        process entity signals with run_count >= 5.
    """

    graph_context: GraphNeighbourhood
    dependency_paths: list[list[str]]
    temporal_support: dict[str, dict[str, Any]]


# ---------------------------------------------------------------------------
# Private graph primitives (temporary in-engine implementation; see module
# docstring for the ENT-4 migration plan)
# ---------------------------------------------------------------------------

_EDGES_WITH_IDS_SQL = """
    SELECT
        er.from_entity_id    AS from_entity_id,
        er.to_entity_id      AS to_entity_id,
        er.relationship_type AS relationship_type,
        er.inferred          AS inferred,
        er.confidence        AS confidence,
        ef.entity_type       AS from_entity_type,
        ef.display_name      AS from_display_name,
        ef.resolution_status AS from_resolution_status,
        et.entity_type       AS to_entity_type,
        et.display_name      AS to_display_name,
        et.resolution_status AS to_resolution_status
    FROM entity_relationships er
    JOIN entities ef ON ef.id = er.from_entity_id AND ef.org_id = er.org_id
    JOIN entities et ON et.id = er.to_entity_id   AND et.org_id = er.org_id
    WHERE er.org_id = ?
      AND (er.from_entity_id = ? OR er.to_entity_id = ?)
"""


def _raw_edges_for_entity(org_id: str, entity_id: str) -> list[dict[str, Any]]:
    """Return all edges, including inferred edges, touching entity_id."""

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_EDGES_WITH_IDS_SQL, (org_id, entity_id, entity_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _depth3_neighbourhood(org_id: str, seed_entity_ids: list[str]) -> GraphNeighbourhood:
    """BFS to depth 3 from seed entities, including inferred edges.

    Causal analysis deliberately includes inferred edges. Stage 3 validates
    them against temporal data.
    """

    visited_entity_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    entities: dict[str, EntityNode] = {}
    edges: list[EdgeNode] = []

    queue: deque[tuple[str, int]] = deque()
    for entity_id in seed_entity_ids:
        if entity_id not in visited_entity_ids:
            visited_entity_ids.add(entity_id)
            queue.append((entity_id, 0))

    while queue:
        current_id, depth = queue.popleft()
        raw_edges = _raw_edges_for_entity(org_id, current_id)

        for row in raw_edges:
            fid = row["from_entity_id"]
            tid = row["to_entity_id"]
            relationship_type = row["relationship_type"]

            neighbour_id = tid if fid == current_id else fid
            neighbour_depth = depth + 1

            if neighbour_id not in visited_entity_ids and neighbour_depth > 3:
                continue

            edge_key = (fid, tid, relationship_type)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append(
                    EdgeNode(
                        from_entity_id=fid,
                        to_entity_id=tid,
                        relationship_type=relationship_type,
                        inferred=bool(row["inferred"]),
                        confidence=float(row["confidence"]),
                    )
                )

            if fid not in entities:
                entities[fid] = EntityNode(
                    entity_id=fid,
                    entity_type=row["from_entity_type"],
                    display_name=row["from_display_name"],
                    resolution_status=row["from_resolution_status"],
                    org_id=org_id,
                )
            if tid not in entities:
                entities[tid] = EntityNode(
                    entity_id=tid,
                    entity_type=row["to_entity_type"],
                    display_name=row["to_display_name"],
                    resolution_status=row["to_resolution_status"],
                    org_id=org_id,
                )

            if neighbour_id not in visited_entity_ids:
                visited_entity_ids.add(neighbour_id)
                queue.append((neighbour_id, neighbour_depth))

    return GraphNeighbourhood(entities=list(entities.values()), edges=edges)


def _shortest_path(
    neighbourhood: GraphNeighbourhood,
    source_id: str,
    target_id: str,
) -> Optional[list[str]]:
    """Bidirectional BFS shortest path between two entity IDs."""

    if source_id == target_id:
        return [source_id]

    adjacency: dict[str, set[str]] = {}
    for edge in neighbourhood.edges:
        adjacency.setdefault(edge.from_entity_id, set()).add(edge.to_entity_id)
        adjacency.setdefault(edge.to_entity_id, set()).add(edge.from_entity_id)

    visited: dict[str, Optional[str]] = {source_id: None}
    queue: deque[str] = deque([source_id])
    while queue:
        current = queue.popleft()
        if current == target_id:
            path: list[str] = []
            node: Optional[str] = target_id
            while node is not None:
                path.append(node)
                node = visited[node]
            path.reverse()
            return path

        for neighbour in adjacency.get(current, set()):
            if neighbour not in visited:
                visited[neighbour] = current
                queue.append(neighbour)

    return None


# ---------------------------------------------------------------------------
# Temporal support assembly
# ---------------------------------------------------------------------------

_MIN_RUNS_FOR_TEMPORAL_SUPPORT = 5


def _build_temporal_support(
    org_id: str,
    pack_id: str,
    process_entities: list[EntityNode],
) -> dict[str, dict[str, Any]]:
    """Assemble trend/anomaly/context for process entity signals with >= 5 runs."""

    support: dict[str, dict[str, Any]] = {}

    for entity in process_entities:
        signal_key = f"{pack_id}::{entity.entity_id}::metric_value"
        try:
            trend = calculate_trend(org_id, signal_key)
        except Exception:
            logger.debug("calculate_trend failed for signal_key=%s", signal_key)
            continue

        if trend.run_count < _MIN_RUNS_FOR_TEMPORAL_SUPPORT:
            continue

        try:
            # Use a representative value; signal history provides the baseline.
            anomaly = calculate_anomaly(org_id, signal_key, current_value=0.0)
        except Exception:
            logger.debug("calculate_anomaly failed for signal_key=%s", signal_key)
            anomaly = None

        context_str: Optional[str] = None
        if anomaly is not None:
            try:
                context_str = build_baseline_context(trend, anomaly, current_value=0.0)
            except Exception:
                logger.debug("build_baseline_context failed for signal_key=%s", signal_key)

        support[signal_key] = {
            "trend": trend.trend_direction,
            "anomaly": anomaly.is_anomalous if anomaly is not None else False,
            "context": context_str,
            "run_count": trend.run_count,
        }

    return support


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_causal_context(
    org_id: str,
    opportunity_id: str,
    seed_entity_ids: list[str],
    pack_id: str,
) -> CausalContext:
    """Assemble graph neighbourhood, dependency paths, and temporal support.

    Raises
    ------
    InsufficientGraphContextError
        When the assembled neighbourhood has fewer than 3 entities (AC9).
    """

    neighbourhood = _depth3_neighbourhood(org_id, seed_entity_ids)

    if neighbourhood.entity_count < 3:
        logger.info(
            "Insufficient graph context for opportunity=%s org=%s entity_count=%d",
            opportunity_id,
            org_id,
            neighbourhood.entity_count,
        )
        raise InsufficientGraphContextError(
            f"opportunity {opportunity_id} has only {neighbourhood.entity_count} "
            "entities in its depth-3 neighbourhood (minimum 3 required)"
        )

    process_entities = [
        entity for entity in neighbourhood.entities if entity.entity_type == "process"
    ]

    dependency_paths: list[list[str]] = []
    for i, process_a in enumerate(process_entities):
        for process_b in process_entities[i + 1:]:
            path = _shortest_path(neighbourhood, process_a.entity_id, process_b.entity_id)
            if path:
                dependency_paths.append(path)

    temporal_support = _build_temporal_support(org_id, pack_id, process_entities)

    return CausalContext(
        graph_context=neighbourhood,
        dependency_paths=dependency_paths,
        temporal_support=temporal_support,
    )


# ---------------------------------------------------------------------------
# T4 - falsifiability validation
# ---------------------------------------------------------------------------

_GENERIC_PHRASES: frozenset[str] = frozenset(
    {
        "if this is wrong",
        "if future data contradicts",
        "if data shows otherwise",
        "if evidence suggests",
        "if proven incorrect",
        "if this hypothesis is incorrect",
        "if this turns out",
        "if further investigation",
        "if additional data",
        "if more data",
        "if this is not the case",
        "if this does not hold",
        "if we are wrong",
        "if the assumption is wrong",
    }
)

_MIN_FALSIFIABILITY_LENGTH = 30

_MEASURABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+\s*%"),
    re.compile(r"\d+\s*-?\s*day"),
    re.compile(r"\bdays?\b"),
    re.compile(r"\bweeks?\b"),
    re.compile(r"\bmonths?\b"),
    re.compile(r"\b\d+\b"),
    re.compile(r"\bbaseline\b"),
    re.compile(r"\bthreshold\b"),
    re.compile(r"\brate\b"),
    re.compile(r"\bcount\b"),
    re.compile(r"\bvolume\b"),
    re.compile(r"\bscore\b"),
    re.compile(r"\breview\s+time\b"),
    re.compile(r"\bcycle\s+time\b"),
    re.compile(r"\bsla\b", re.I),
    re.compile(r"\bbacklog\b"),
    re.compile(r"\bqueue\b"),
    re.compile(r"\blatency\b"),
)


def is_generic_falsifiability(text: str, causal_context: Optional[Any] = None) -> bool:
    """Return True when a falsifiability condition is semantically empty."""

    if not text or not text.strip():
        return True

    stripped = text.strip()
    if len(stripped) < _MIN_FALSIFIABILITY_LENGTH:
        return True

    lower = stripped.lower()
    for phrase in _GENERIC_PHRASES:
        if phrase in lower:
            return True

    for pattern in _MEASURABLE_PATTERNS:
        if pattern.search(stripped):
            return False

    if causal_context is not None:
        try:
            for entity in causal_context.graph_context.entities:
                name = (entity.display_name or "").strip()
                if name and name.lower() in lower:
                    return False
        except Exception:
            pass

    return True


# ---------------------------------------------------------------------------
# T4 - hallucination guard for cause-chain steps
# ---------------------------------------------------------------------------

_SENTENCE_OPENERS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "at",
        "in",
        "on",
        "to",
        "of",
        "or",
        "and",
        "but",
        "for",
        "with",
        "by",
        "from",
        "into",
        "over",
        "when",
        "if",
        "then",
        "after",
        "before",
        "because",
        "since",
        "while",
        "although",
        "however",
        "therefore",
        "thus",
        "hence",
        "step",
        "observed",
        "inferred",
    }
)


def _extract_proper_noun_tokens(text: str) -> set[str]:
    """Extract multi-word capitalised spans as candidate entity names."""

    tokens = re.split(r"\s+", text.strip())
    cleaned = [re.sub(r"^[^\w]+|[^\w]+$", "", token) for token in tokens]

    spans: set[str] = set()
    run: list[str] = []

    for token in cleaned:
        if not token:
            run = []
            continue
        if token[0].isupper() and token.lower() not in _SENTENCE_OPENERS:
            run.append(token)
        else:
            if len(run) >= 2:
                spans.add(" ".join(run))
            run = []

    if len(run) >= 2:
        spans.add(" ".join(run))

    return spans


def _context_entity_names(causal_context: Any) -> set[str]:
    """Return display names from the causal context, lower-cased."""

    names: set[str] = set()
    try:
        for entity in causal_context.graph_context.entities:
            name = (entity.display_name or "").strip()
            if name:
                names.add(name.lower())
                for part in name.split():
                    if len(part) > 2:
                        names.add(part.lower())
    except Exception:
        pass
    return names


def _apply_hallucination_guard(
    steps: list[str],
    causal_context: Optional[Any],
) -> list[str]:
    """Remove steps whose proper-noun tokens are not in the causal context."""

    if causal_context is None:
        return steps

    known_names = _context_entity_names(causal_context)
    if not known_names:
        return steps

    clean_steps: list[str] = []
    for step in steps:
        proper_nouns = _extract_proper_noun_tokens(step)
        if not proper_nouns:
            clean_steps.append(step)
            continue

        hallucinated = [
            proper_noun
            for proper_noun in proper_nouns
            if proper_noun.lower() not in known_names
            and not any(word.lower() in known_names for word in proper_noun.split())
        ]
        if hallucinated:
            logger.debug(
                "Hallucination guard: removing step with unverifiable entities %s",
                hallucinated,
            )
        else:
            clean_steps.append(step)

    return clean_steps


# ---------------------------------------------------------------------------
# T4 - parse_causal_output
# ---------------------------------------------------------------------------

def _reject(reason: str, org_id: str, run_id: str, opportunity_id: str) -> None:
    """Fire causal.hypothesis_rejected telemetry. Never raises."""

    try:
        from app.telemetry import record_event
    except ModuleNotFoundError:  # pragma: no cover
        try:
            from backend.app.telemetry import record_event  # type: ignore[no-redef]
        except ModuleNotFoundError:
            logger.warning(
                "telemetry unavailable; skipping causal.hypothesis_rejected(%s)",
                reason,
            )
            return

    try:
        record_event(
            "causal.hypothesis_rejected",
            {
                "reason": reason,
                "org_id": org_id,
                "run_id": run_id,
                "opportunity_id": opportunity_id,
            },
        )
    except Exception as exc:
        logger.warning("record_event causal.hypothesis_rejected failed: %s", exc)


def parse_causal_output(
    llm_response: Any,
    *,
    org_id: str,
    run_id: str,
    opportunity_id: str,
    causal_context: Optional[Any],
) -> Optional[dict[str, Any]]:
    """Parse and validate the LLM's causal hypothesis output.

    On success returns {'cause_chain': [...], 'falsifiability_condition': '...'}.
    Never raises; unexpected parser failures become no_falsifiability rejections.
    """

    try:
        return _parse_causal_output_inner(
            llm_response,
            org_id=org_id,
            run_id=run_id,
            opportunity_id=opportunity_id,
            causal_context=causal_context,
        )
    except Exception as exc:
        logger.warning(
            "parse_causal_output unexpected error for opp=%s: %s - treating as no_falsifiability",
            opportunity_id,
            exc,
        )
        _reject("no_falsifiability", org_id, run_id, opportunity_id)
        return None


def _parse_causal_output_inner(
    llm_response: Any,
    *,
    org_id: str,
    run_id: str,
    opportunity_id: str,
    causal_context: Optional[Any],
) -> Optional[dict[str, Any]]:
    """Core implementation of parse_causal_output."""

    if not isinstance(llm_response, dict):
        _reject("no_falsifiability", org_id, run_id, opportunity_id)
        return None

    cause_chain = llm_response.get("cause_chain")
    falsifiability_condition = llm_response.get("falsifiability_condition")

    if not cause_chain or not isinstance(cause_chain, list):
        logger.info("parse_causal_output: cause_chain absent for opp=%s", opportunity_id)
        _reject("no_falsifiability", org_id, run_id, opportunity_id)
        return None

    if not falsifiability_condition or not str(falsifiability_condition).strip():
        logger.info(
            "parse_causal_output: falsifiability_condition absent for opp=%s",
            opportunity_id,
        )
        _reject("no_falsifiability", org_id, run_id, opportunity_id)
        return None

    falsifiability_text = str(falsifiability_condition).strip()

    if len(cause_chain) > 5:
        logger.warning(
            "parse_causal_output: cause_chain has %d steps for opp=%s - truncating to 5",
            len(cause_chain),
            opportunity_id,
        )
        cause_chain = cause_chain[:5]

    clean_steps = [str(step).strip() for step in cause_chain if str(step).strip()]

    if not clean_steps:
        logger.info(
            "parse_causal_output: empty cause_chain after filtering for opp=%s",
            opportunity_id,
        )
        _reject("empty_cause_chain", org_id, run_id, opportunity_id)
        return None

    if is_generic_falsifiability(falsifiability_text, causal_context):
        logger.info(
            "parse_causal_output: generic falsifiability_condition for opp=%s: %r",
            opportunity_id,
            falsifiability_text[:80],
        )
        _reject("generic_falsifiability", org_id, run_id, opportunity_id)
        return None

    guarded_steps = _apply_hallucination_guard(clean_steps, causal_context)
    if len(guarded_steps) < 2:
        logger.info(
            "parse_causal_output: hallucination guard left %d step(s) for opp=%s - rejecting",
            len(guarded_steps),
            opportunity_id,
        )
        _reject("hallucination_in_cause_chain", org_id, run_id, opportunity_id)
        return None

    return {
        "cause_chain": guarded_steps,
        "falsifiability_condition": falsifiability_text,
    }
