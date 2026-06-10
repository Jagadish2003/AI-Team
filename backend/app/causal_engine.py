"""Causal inference engine for Stage 3 (T3-S16-A / ENT-6).

This module owns:
  CausalContext               — assembled data package fed to the causal-chain prompt (T2).
  InsufficientGraphContextError — raised when entity count < 3 (AC9).
  build_causal_context()      — assembles graph neighbourhood, dependency paths,
                                and temporal support for process entities (T2).
  is_generic_falsifiability() — heuristic that flags semantically empty conditions (T4).
  parse_causal_output()       — parses and validates LLM causal output (T4).

ENT-4 dependency note (T2)
--------------------------
Section 2a specifies opportunity_neighbourhood() and entity_path() from
graph_query.py as the intended graph API.  Those functions are ENT-4
deliverables and are not present in the current branch — graph_query.py
today only exposes get_entity_relationships(org_id, entity_id, inferred=False).

Decision (option a): this module provides a minimal, clearly-scoped in-engine
implementation of both primitives on top of get_entity_relationships():
  _depth3_neighbourhood() — BFS to depth 3 with inferred=True.
  _shortest_path()        — bidirectional BFS between two entity IDs.

When ENT-4 merges and graph_query.py exposes opportunity_neighbourhood() /
entity_path(), replace the two private helpers with imports and delete this note.
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
    from app.trend_engine import calculate_anomaly, calculate_trend
    from app.temporal_enrichment import build_baseline_context
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports
    from backend.app import db  # type: ignore[no-redef]
    from backend.app.trend_engine import calculate_anomaly, calculate_trend  # type: ignore[no-redef]
    from backend.app.temporal_enrichment import build_baseline_context  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Public exceptions
# ─────────────────────────────────────────────────────────────────────────────

class InsufficientGraphContextError(Exception):
    """Raised when the entity neighbourhood has fewer than 3 entities (AC9).

    Callers catch this, skip hypothesis generation, and log
    causal.hypothesis_rejected with reason='insufficient_graph_context'.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Graph neighbourhood data containers (T2)
# ─────────────────────────────────────────────────────────────────────────────

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
    entities: list = field(default_factory=list)
    edges: list = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        return len(self.entities)


# ─────────────────────────────────────────────────────────────────────────────
# CausalContext (T2)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CausalContext:
    """All data needed to generate and validate a causal-chain hypothesis.

    Fields
    ------
    graph_context     : depth-3 entity/edge neighbourhood (inferred edges included).
    dependency_paths  : pairwise shortest paths between process entities.
    temporal_support  : signal_key → {trend, anomaly, context, run_count}.
    """
    graph_context: GraphNeighbourhood
    dependency_paths: list
    temporal_support: dict


# ─────────────────────────────────────────────────────────────────────────────
# Private graph primitives (T2 — temporary in-engine, pending ENT-4)
# ─────────────────────────────────────────────────────────────────────────────

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


def _raw_edges_for_entity(org_id: str, entity_id: str) -> list:
    """Return all edges (including inferred) touching entity_id."""
    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_EDGES_WITH_IDS_SQL, (org_id, entity_id, entity_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _depth3_neighbourhood(org_id: str, seed_entity_ids: list) -> GraphNeighbourhood:
    """BFS to depth 3 from seed entities, including inferred edges.

    Causal analysis deliberately includes inferred edges — Stage 3 is the
    step that validates them against temporal data (spec Section 2a).
    """
    visited_entity_ids: set = set()
    seen_edges: set = set()

    entities: dict = {}
    edges: list = []

    queue: deque = deque()
    for eid in seed_entity_ids:
        if eid not in visited_entity_ids:
            visited_entity_ids.add(eid)
            queue.append((eid, 0))

    while queue:
        current_id, depth = queue.popleft()
        raw_edges = _raw_edges_for_entity(org_id, current_id)

        for row in raw_edges:
            fid = row["from_entity_id"]
            tid = row["to_entity_id"]
            rtype = row["relationship_type"]

            neighbour_id = tid if fid == current_id else fid
            neighbour_depth = depth + 1

            if neighbour_id not in visited_entity_ids and neighbour_depth > 3:
                continue

            edge_key = (fid, tid, rtype)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append(EdgeNode(
                    from_entity_id=fid,
                    to_entity_id=tid,
                    relationship_type=rtype,
                    inferred=bool(row["inferred"]),
                    confidence=float(row["confidence"]),
                ))

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
) -> Optional[list]:
    """Bidirectional BFS shortest path between two entity IDs in the neighbourhood."""
    if source_id == target_id:
        return [source_id]

    adjacency: dict = {}
    for edge in neighbourhood.edges:
        adjacency.setdefault(edge.from_entity_id, set()).add(edge.to_entity_id)
        adjacency.setdefault(edge.to_entity_id, set()).add(edge.from_entity_id)

    visited: dict = {source_id: None}
    queue: deque = deque([source_id])
    while queue:
        current = queue.popleft()
        if current == target_id:
            path: list = []
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


# ─────────────────────────────────────────────────────────────────────────────
# Temporal support assembly (T2)
# ─────────────────────────────────────────────────────────────────────────────

_MIN_RUNS_FOR_TEMPORAL_SUPPORT = 5


def _build_temporal_support(
    org_id: str,
    pack_id: str,
    process_entities: list,
) -> dict:
    support: dict = {}

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


# ─────────────────────────────────────────────────────────────────────────────
# build_causal_context (T2 public entry point)
# ─────────────────────────────────────────────────────────────────────────────

def build_causal_context(
    org_id: str,
    opportunity_id: str,
    seed_entity_ids: list,
    pack_id: str,
) -> CausalContext:
    """Assemble graph neighbourhood, dependency paths, and temporal support.

    Raises InsufficientGraphContextError when neighbourhood < 3 entities (AC9).
    """
    neighbourhood = _depth3_neighbourhood(org_id, seed_entity_ids)

    if neighbourhood.entity_count < 3:
        logger.info(
            "Insufficient graph context for opportunity=%s org=%s entity_count=%d",
            opportunity_id, org_id, neighbourhood.entity_count,
        )
        raise InsufficientGraphContextError(
            f"opportunity {opportunity_id} has only {neighbourhood.entity_count} "
            "entities in its depth-3 neighbourhood (minimum 3 required)"
        )

    process_entities = [
        e for e in neighbourhood.entities if e.entity_type == "process"
    ]

    dependency_paths: list = []
    for i, pa in enumerate(process_entities):
        for pb in process_entities[i + 1:]:
            path = _shortest_path(neighbourhood, pa.entity_id, pb.entity_id)
            if path:
                dependency_paths.append(path)

    temporal_support = _build_temporal_support(org_id, pack_id, process_entities)

    return CausalContext(
        graph_context=neighbourhood,
        dependency_paths=dependency_paths,
        temporal_support=temporal_support,
    )


# ─────────────────────────────────────────────────────────────────────────────
# T4 — Falsifiability validation
# ─────────────────────────────────────────────────────────────────────────────

# Known generic phrases that are semantically empty as falsifiability conditions.
# Each entry is a lowercased substring to search for. Adding a phrase here means
# any condition containing it as a substring is flagged as generic regardless of
# surrounding words.
_GENERIC_PHRASES: frozenset = frozenset({
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
})

# Minimum character length for a non-trivially-generic falsifiability condition.
_MIN_FALSIFIABILITY_LENGTH = 30

# Regex patterns that indicate a measurable qualifier is present.
# These make the heuristic conservative — we only flag as generic when
# NONE of these markers appear.
_MEASURABLE_PATTERNS: tuple = (
    re.compile(r"\d+\s*%"),             # percentage: "40%", "40 %"
    re.compile(r"\d+\s*-?\s*day"),      # time period: "90-day", "30 days"
    re.compile(r"\bdays?\b"),           # "days" / "day"
    re.compile(r"\bweeks?\b"),          # "weeks"
    re.compile(r"\bmonths?\b"),         # "months"
    re.compile(r"\b\d+\b"),             # any bare number
    re.compile(r"\bbaseline\b"),        # "baseline" is inherently measurable
    re.compile(r"\bthreshold\b"),       # "threshold"
    re.compile(r"\brate\b"),            # "rate" implies a metric
    re.compile(r"\bcount\b"),           # "count"
    re.compile(r"\bvolume\b"),          # "volume"
    re.compile(r"\bscore\b"),           # "score"
    re.compile(r"\breview\s+time\b"),   # "review time"
    re.compile(r"\bcycle\s+time\b"),    # "cycle time"
    re.compile(r"\bsla\b", re.I),       # SLA
    re.compile(r"\bbacklog\b"),         # "backlog"
    re.compile(r"\bqueue\b"),           # "queue"
    re.compile(r"\blatency\b"),         # "latency"
)


def is_generic_falsifiability(text: str, causal_context: Optional[Any] = None) -> bool:
    """Return True when a falsifiability condition is semantically empty.

    A condition is flagged as generic when ANY of the following hold:
      1. Trimmed text is shorter than _MIN_FALSIFIABILITY_LENGTH characters.
      2. Text contains a known generic phrase (see _GENERIC_PHRASES).
      3. Text contains no measurable qualifier: no percentage, no numeric threshold,
         no named metric keyword, no time period, AND no named entity from
         causal_context (when provided).

    The heuristic is intentionally conservative and deterministic:
    - It only rejects on clear signals of semantic emptiness.
    - Passing causal_context allows entity-name matching as an escape hatch —
      a condition naming a real entity (e.g. "Sarah Chen's caseload") is
      considered measurable even without a number.
    - The goal is to reject "if this is wrong" style conditions, not to perform
      NLP-level semantic analysis.
    """
    if not text or not text.strip():
        return True

    stripped = text.strip()

    # Rule 1: too short to convey any specificity
    if len(stripped) < _MIN_FALSIFIABILITY_LENGTH:
        return True

    lower = stripped.lower()

    # Rule 2: known generic phrase
    for phrase in _GENERIC_PHRASES:
        if phrase in lower:
            return True

    # Rule 3: no measurable qualifier
    for pattern in _MEASURABLE_PATTERNS:
        if pattern.search(stripped):
            return False  # found a measurable qualifier — not generic

    # Check entity names from context as a measurable qualifier escape hatch
    if causal_context is not None:
        try:
            for entity in causal_context.graph_context.entities:
                name = (entity.display_name or "").strip()
                if name and name.lower() in lower:
                    return False
        except Exception:
            pass

    return True  # no measurable qualifier found


# ─────────────────────────────────────────────────────────────────────────────
# T4 — Hallucination guard for cause chain steps
# ─────────────────────────────────────────────────────────────────────────────

# Common sentence-opening words that are capitalised for grammatical reasons,
# not because they are entity names. These are excluded from proper-noun detection.
_SENTENCE_OPENERS: frozenset = frozenset({
    # Generic English grammatical/structural words that are capitalised only at
    # sentence start, never as part of an entity name. Domain terms (Credit,
    # Loan, Covenant, Jira etc.) are intentionally excluded — they can appear
    # legitimately in multi-word entity names like "Commercial Credit Team".
    "the", "a", "an", "this", "that", "these", "those", "it", "its",
    "as", "at", "in", "on", "to", "of", "or", "and", "but", "for",
    "with", "by", "from", "into", "over", "when", "if", "then",
    "after", "before", "because", "since", "while", "although",
    "however", "therefore", "thus", "hence", "step",
    "observed", "inferred",
})


def _extract_proper_noun_tokens(text: str) -> set:
    """Extract multi-word capitalised spans from a step as candidate entity names.

    Rule: sequences of 2 or more consecutive Title-cased words not in the
    sentence-opener exclusion list. Single capitalised words are excluded
    because they are commonly sentence starters, not entity names.

    Example: "Sarah Chen owns 4 overdue reviews" → {"Sarah Chen"}
    Example: "Loan origination rose 40%" → {} (no multi-word Title span)

    This is intentionally simple and deterministic — full NLP is not needed
    for the guard to fulfil its purpose of stripping names the graph cannot vouch for.
    """
    # Split into whitespace-separated tokens; strip punctuation from each.
    tokens = re.split(r"\s+", text.strip())
    cleaned = [re.sub(r"^[^\w]+|[^\w]+$", "", t) for t in tokens]

    spans: set = set()
    run: list = []

    for tok in cleaned:
        if not tok:
            run = []
            continue
        if tok[0].isupper() and tok.lower() not in _SENTENCE_OPENERS:
            run.append(tok)
        else:
            if len(run) >= 2:
                spans.add(" ".join(run))
            run = []

    if len(run) >= 2:
        spans.add(" ".join(run))

    return spans


def _context_entity_names(causal_context: Any) -> set:
    """Return the set of all display names from the causal context (lower-cased)."""
    names: set = set()
    try:
        for entity in causal_context.graph_context.entities:
            name = (entity.display_name or "").strip()
            if name:
                names.add(name.lower())
                # Also add each word of the name individually so partial matches work
                for part in name.split():
                    if len(part) > 2:
                        names.add(part.lower())
    except Exception:
        pass
    return names


def _apply_hallucination_guard(steps: list, causal_context: Optional[Any]) -> list:
    """Remove steps whose proper-noun tokens are not in the causal context.

    When causal_context is None, the guard is skipped and all steps pass.
    A step passes the guard when:
      - It contains no multi-word capitalised spans (no named entity claim), OR
      - All of its capitalised spans appear in the context entity names.

    Steps with at least one capitalised span not found in the context are removed.
    """
    if causal_context is None:
        return steps

    known_names = _context_entity_names(causal_context)
    if not known_names:
        # No resolved entities in context — skip guard to avoid false positives
        return steps

    clean_steps: list = []
    for step in steps:
        proper_nouns = _extract_proper_noun_tokens(step)
        if not proper_nouns:
            # No proper-noun claims — step passes unconditionally
            clean_steps.append(step)
            continue
        # Check every extracted span against the known names (case-insensitive)
        hallucinated = [
            pn for pn in proper_nouns
            if pn.lower() not in known_names
            # Also check if any word of the span is a known entity word
            and not any(word.lower() in known_names for word in pn.split())
        ]
        if hallucinated:
            logger.debug(
                "Hallucination guard: removing step with unverifiable entities %s",
                hallucinated,
            )
        else:
            clean_steps.append(step)

    return clean_steps


# ─────────────────────────────────────────────────────────────────────────────
# T4 — parse_causal_output
# ─────────────────────────────────────────────────────────────────────────────

def _reject(reason: str, org_id: str, run_id: str, opportunity_id: str) -> None:
    """Fire causal.hypothesis_rejected telemetry. Never raises."""
    try:
        from app.telemetry import record_event
    except ModuleNotFoundError:  # pragma: no cover
        try:
            from backend.app.telemetry import record_event  # type: ignore[no-redef]
        except ModuleNotFoundError:
            logger.warning("telemetry unavailable; skipping causal.hypothesis_rejected(%s)", reason)
            return
    try:
        record_event("causal.hypothesis_rejected", {
            "reason": reason,
            "org_id": org_id,
            "run_id": run_id,
            "opportunity_id": opportunity_id,
        })
    except Exception as exc:
        logger.warning("record_event causal.hypothesis_rejected failed: %s", exc)


def parse_causal_output(
    llm_response: dict,
    *,
    org_id: str,
    run_id: str,
    opportunity_id: str,
    causal_context: Optional[Any],
) -> Optional[dict]:
    """Parse and validate the LLM's causal hypothesis output.

    Extracts cause_chain and falsifiability_condition from llm_response (the
    same dict shape _parse_json() produces in llm_enrichment.py).

    Validation pipeline (each rejection fires causal.hypothesis_rejected):
      1. cause_chain or falsifiability_condition absent / empty
         → reason='no_falsifiability', return None.
      2. cause_chain > 5 steps → truncate to 5 with a warning (no rejection).
      3. Filter empty strings from cause_chain.
      4. Filtered chain is empty → reason='empty_cause_chain', return None.
      5. is_generic_falsifiability(falsifiability_condition)
         → reason='generic_falsifiability', return None.
      6. Hallucination guard: steps with unverifiable entity names removed.
         Fewer than 2 steps survive → reason='hallucination_in_cause_chain', return None.

    On success returns {'cause_chain': [...], 'falsifiability_condition': '...'}.
    Never raises — all exceptions are caught and treated as no_falsifiability.

    Parameters
    ----------
    llm_response    : the parsed JSON dict from the LLM (may contain other fields).
    org_id          : organisation scope — included in rejection telemetry payload.
    run_id          : run identifier — included in rejection telemetry payload.
    opportunity_id  : opportunity being explained.
    causal_context  : CausalContext from build_causal_context(), or None when
                      unavailable. Used by the hallucination guard and the
                      measurable-entity escape hatch in is_generic_falsifiability().
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
            "parse_causal_output unexpected error for opp=%s: %s — treating as no_falsifiability",
            opportunity_id, exc,
        )
        _reject("no_falsifiability", org_id, run_id, opportunity_id)
        return None


def _parse_causal_output_inner(
    llm_response: dict,
    *,
    org_id: str,
    run_id: str,
    opportunity_id: str,
    causal_context: Optional[Any],
) -> Optional[dict]:
    """Core implementation of parse_causal_output — may raise; caller wraps."""
    if not isinstance(llm_response, dict):
        _reject("no_falsifiability", org_id, run_id, opportunity_id)
        return None

    cause_chain = llm_response.get("cause_chain")
    falsifiability_condition = llm_response.get("falsifiability_condition")

    # Step 1 — presence and non-emptiness check
    if not cause_chain or not isinstance(cause_chain, list):
        logger.info(
            "parse_causal_output: cause_chain absent for opp=%s", opportunity_id
        )
        _reject("no_falsifiability", org_id, run_id, opportunity_id)
        return None

    if not falsifiability_condition or not str(falsifiability_condition).strip():
        logger.info(
            "parse_causal_output: falsifiability_condition absent for opp=%s",
            opportunity_id,
        )
        _reject("no_falsifiability", org_id, run_id, opportunity_id)
        return None

    falsifiability_condition = str(falsifiability_condition).strip()

    # Step 2 — length cap: truncate >5 steps with a warning, do not reject
    if len(cause_chain) > 5:
        logger.warning(
            "parse_causal_output: cause_chain has %d steps for opp=%s — truncating to 5",
            len(cause_chain), opportunity_id,
        )
        cause_chain = cause_chain[:5]

    # Step 3 — filter empty/non-string steps
    clean_steps = [str(s).strip() for s in cause_chain if str(s).strip()]

    # Step 4 — empty after filtering
    if not clean_steps:
        logger.info(
            "parse_causal_output: empty cause_chain after filtering for opp=%s",
            opportunity_id,
        )
        _reject("empty_cause_chain", org_id, run_id, opportunity_id)
        return None

    # Step 5 — generic falsifiability
    if is_generic_falsifiability(falsifiability_condition, causal_context):
        logger.info(
            "parse_causal_output: generic falsifiability_condition for opp=%s: %r",
            opportunity_id, falsifiability_condition[:80],
        )
        _reject("generic_falsifiability", org_id, run_id, opportunity_id)
        return None

    # Step 6 — hallucination guard
    guarded_steps = _apply_hallucination_guard(clean_steps, causal_context)
    if len(guarded_steps) < 2:
        logger.info(
            "parse_causal_output: hallucination guard left %d step(s) for opp=%s — rejecting",
            len(guarded_steps), opportunity_id,
        )
        _reject("hallucination_in_cause_chain", org_id, run_id, opportunity_id)
        return None

    return {
        "cause_chain": guarded_steps,
        "falsifiability_condition": falsifiability_condition,
    }
