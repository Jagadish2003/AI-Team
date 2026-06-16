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

try:
    from database.models.causal_hypotheses import CausalHypothesis
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports
    from backend.database.models.causal_hypotheses import CausalHypothesis  # type: ignore[no-redef]

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
    WHERE er.org_id = %s
      AND (er.from_entity_id = %s OR er.to_entity_id = %s)
"""


def _raw_edges_for_entity(org_id: str, entity_id: str) -> list[dict[str, Any]]:
    """Return all edges, including inferred edges, touching entity_id."""

    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_EDGES_WITH_IDS_SQL, (org_id, entity_id, entity_id))
        rows = cur.fetchall()
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


def _candidate_signal_keys(pack_id: str, entity: EntityNode) -> list[str]:
    """Signal keys that may represent a process entity's temporal history."""
    candidates: list[str] = []
    for middle in (entity.display_name, entity.entity_id):
        middle = (middle or "").strip()
        if not middle:
            continue
        signal_key = f"{pack_id}::{middle}::metric_value"
        if signal_key not in candidates:
            candidates.append(signal_key)
    return candidates


def _build_temporal_support(
    org_id: str,
    pack_id: str,
    process_entities: list[EntityNode],
) -> dict[str, dict[str, Any]]:
    """Assemble trend/anomaly/context for process entity signals with >= 5 runs."""

    support: dict[str, dict[str, Any]] = {}

    for entity in process_entities:
        signal_key = ""
        trend = None
        for candidate_signal_key in _candidate_signal_keys(pack_id, entity):
            try:
                candidate_trend = calculate_trend(org_id, candidate_signal_key)
            except Exception:
                logger.debug("calculate_trend failed for signal_key=%s", candidate_signal_key)
                continue
            if candidate_trend.run_count < _MIN_RUNS_FOR_TEMPORAL_SUPPORT:
                continue
            signal_key = candidate_signal_key
            trend = candidate_trend
            break
        if trend is None:
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


# ---------------------------------------------------------------------------
# T5 - causal quality gates (ENT-6 Section 1)
# ---------------------------------------------------------------------------
#
# evaluate_causal_quality_gates() decides whether a validated causal hypothesis
# is stored CONFIRMED (preliminary=False) or PRELIMINARY (preliminary=True). It
# is the central commitment of ENT-6 (Sections 1 and 8).
#
# The three gates are deliberately NOT configurable. There is intentionally no
# flag, env var, threshold parameter, or "demo mode" that can bypass or weaken
# any gate - the preliminary banner is the honest answer, not a limitation. If a
# future change appears to require a bypass, stop and escalate rather than
# adding one here.
#
# The function is PURE: it performs read-only lookups and never writes to the
# database, so it is trivially unit-testable. All persistence - including the
# gate_run_count column - is owned by T6's store_causal_hypothesis(), which
# consumes the (preliminary, reason) result plus the threaded run_count below.

# Gate 1 threshold - hardcoded by design (see note above). Causal chains built
# on fewer than 10 runs rest on data that has not had time to stabilise.
GATE1_MIN_RUN_COUNT = 10

# Marker the T3 prompt (Rule 2) instructs the LLM to prepend to any cause-chain
# step whose primary evidence is an inferred (confidence < 0.8) relationship,
# e.g. "3. [inferred: 0.6] Backlog pressure ...". Matching INFERRED_CONFIDENCE =
# 0.6 in entity_relationships.py. Tolerant of leading whitespace and casing.
_INFERRED_STEP_LABEL_RE = re.compile(r"\[\s*inferred\b", re.IGNORECASE)


class GateResult(tuple):
    """Result of evaluate_causal_quality_gates().

    Behaves exactly like the documented ``tuple[bool, str | None]`` return -
    ``preliminary, reason = result`` and ``result == (preliminary, reason)`` both
    work - while additionally carrying ``run_count``, the live primary-signal run
    count Gate 1 observed. T6 reads ``result.run_count`` to persist
    gate_run_count as *exactly* the value the gates saw, never a second
    (possibly drifted) read of signal_snapshots.
    """

    # NB: a tuple subclass cannot declare a non-empty __slots__ (tuple stores its
    # items in the variable part), so _run_count lives on the instance dict.

    def __new__(cls, preliminary: bool, reason: Optional[str], run_count: int) -> "GateResult":
        self = super().__new__(cls, (bool(preliminary), reason))
        self._run_count = int(run_count)
        return self

    @property
    def preliminary(self) -> bool:
        return self[0]

    @property
    def reason(self) -> Optional[str]:
        return self[1]

    @property
    def run_count(self) -> int:
        return self._run_count

    def __repr__(self) -> str:
        return (
            f"GateResult(preliminary={self.preliminary!r}, "
            f"reason={self.reason!r}, run_count={self.run_count!r})"
        )


# ---------------------------------------------------------------------------
# Input accessors - tolerate dicts or objects so T6 can wire any payload shape
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or object, returning ``default`` when absent."""

    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _lookup(key: str, *sources: Any) -> Any:
    """Return the first non-None value of ``key`` across ``sources``."""

    for source in sources:
        value = _get(source, key)
        if value is not None:
            return value
    return None


def _entities_of(enrichment: Any) -> list[Any]:
    """Return OppEnrichment.entities as a list (dict or object form), else []."""

    entities = _get(enrichment, "entities")
    return list(entities) if entities else []


def _entity_id_of(entity: Any) -> Optional[str]:
    """Best-effort entity id from a bare string, dict, or object."""

    if entity is None:
        return None
    if isinstance(entity, str):
        return entity
    eid = _get(entity, "entity_id") or _get(entity, "id")
    return str(eid) if eid else None


def _entity_ids(value: Any) -> list[str]:
    """Extract entity ids from a list of strings / dicts / objects."""

    if not value:
        return []
    ids: list[str] = []
    for item in value:
        eid = _entity_id_of(item)
        if eid:
            ids.append(eid)
    return ids


def _resolve_org_id(opp: Any, enrichment: Any, causal_context: Any) -> str:
    """Derive org_id from the inputs.

    The documented signature has no org_id parameter, so it is resolved from the
    payloads. Causal-context entities carry org_id from the entities JOIN and are
    the most reliable source; opp/enrichment are fallbacks.
    """

    try:
        for entity in causal_context.graph_context.entities:
            org_id = getattr(entity, "org_id", None)
            if org_id:
                return str(org_id)
    except Exception:
        pass

    org_id = _lookup("org_id", opp, enrichment) or _lookup("orgId", opp, enrichment)
    return str(org_id) if org_id else ""


# ---------------------------------------------------------------------------
# Read-only data sources (monkeypatched in unit tests)
# ---------------------------------------------------------------------------

def _primary_signal_run_count(org_id: str, signal_key: Optional[str]) -> int:
    """Live count of signal_snapshots rows for the primary signal_key (Gate 1).

    Reads the *current* count straight from signal_snapshots - never
    gate_run_count from a causal_hypotheses row, which would be circular: that
    row does not exist yet when the gates run. calculate_trend() is unsuitable
    here because its run_count is capped at the 5-run trend window, while Gate 1
    needs the full history count (>= 10).
    """

    if not signal_key:
        return 0
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM signal_snapshots WHERE org_id = %s AND signal_key = %s",
            (org_id, signal_key),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def _entity_resolution_status_map(org_id: str, entity_ids: list[str]) -> dict[str, str]:
    """Return {entity_id: resolution_status} for ids found in the entities table."""

    if not entity_ids:
        return {}
    conn = db.connect()
    try:
        cur = conn.cursor()
        placeholders = ", ".join("%s" for _ in entity_ids)
        cur.execute(
            f"SELECT id, resolution_status FROM entities "
            f"WHERE org_id = %s AND id IN ({placeholders})",
            (org_id, *entity_ids),
        )
        rows = cur.fetchall()
        return {row["id"]: row["resolution_status"] for row in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Inferred-step detection - the single source of truth shared with T6
# ---------------------------------------------------------------------------

def step_references_inferred_relationship(step: Any) -> bool:
    """True when a cause-chain step is labelled as resting on an inferred edge.

    Detects the ``[inferred: ...]`` marker the T3 prompt (Rule 2) tells the LLM
    to prepend to any step whose primary evidence is an inferred relationship.
    This is the single source of truth for "does this step rely on an inferred
    relationship": Gate 3 and T6's causal_hypotheses.inferred column both call
    it, so the gate decision and the stored flag can never disagree.
    """

    if not step:
        return False
    return bool(_INFERRED_STEP_LABEL_RE.search(str(step)))


def cause_chain_uses_inferred(cause_chain: Any) -> bool:
    """True when any step in the chain references an inferred relationship.

    Convenience for T6 to populate the causal_hypotheses.inferred column from
    the same detector Gate 3 uses.
    """

    if not cause_chain:
        return False
    return any(step_references_inferred_relationship(step) for step in cause_chain)


# ---------------------------------------------------------------------------
# Individual gate evaluators
# ---------------------------------------------------------------------------

def _gate2_unresolved_count(opp: Any, enrichment: Any, org_id: str) -> int:
    """Count distinct entities in the chain that are not 'resolved' (Gate 2).

    Sources: every entity in OppEnrichment.entities plus every id in the
    hypothesis evidence_links. resolution_status is read inline when the entity
    object carries it (OppEnrichment entities do), otherwise looked up in the
    entities table (bare evidence_links ids). Any status other than 'resolved' -
    including an id absent from the table - counts as unresolved, because an
    entity we cannot confirm as resolved cannot anchor a confirmed causal claim.
    """

    # (entity_id, inline_status); inline_status is None when the ref is a bare id.
    refs: list[tuple[Optional[str], Optional[str]]] = []
    for entity in _entities_of(enrichment):
        refs.append((_entity_id_of(entity), _get(entity, "resolution_status")))
    for eid in _entity_ids(_lookup("evidence_links", opp, enrichment)):
        refs.append((eid, None))

    if not refs:
        return 0

    lookup_ids = sorted({eid for eid, status in refs if eid and status is None})
    if lookup_ids:
        try:
            status_map = _entity_resolution_status_map(org_id, lookup_ids)
        except Exception as exc:  # conservative: unknown status -> treat as unresolved
            logger.warning("Gate 2 resolution lookup failed: %s", exc)
            status_map = {}
    else:
        status_map = {}

    non_resolved_ids: set[str] = set()
    anonymous_unresolved = 0
    for eid, inline_status in refs:
        status = inline_status if inline_status is not None else status_map.get(eid)
        if status != "resolved":
            if eid:
                non_resolved_ids.add(eid)
            else:
                anonymous_unresolved += 1
    return len(non_resolved_ids) + anonymous_unresolved


def _gate3_inferred_step_index(opp: Any, enrichment: Any) -> Optional[int]:
    """1-based index of the first cause-chain step that rests on an inferred
    relationship, or None when no step does (Gate 3)."""

    cause_chain = _lookup("cause_chain", opp, enrichment) or []
    for index, step in enumerate(cause_chain, start=1):
        if step_references_inferred_relationship(step):
            return index
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def evaluate_causal_quality_gates(
    opp: Any,
    signal_key: Optional[str],
    opportunity_id: str,
    enrichment: Any,
    causal_context: Any,
) -> GateResult:
    """Evaluate the three ENT-6 causal quality gates (Section 1).

    Returns a GateResult that unpacks as ``(preliminary, reason)``:
      * ``(False, None)``  - all three gates pass; the hypothesis is CONFIRMED.
      * ``(True, reason)`` - at least one gate failed; the hypothesis is stored
        PRELIMINARY and the evidence trace renders the analyst-review banner.

    All three gates are always evaluated (no short-circuit on first failure).
    When more than one fails, the surfaced reason follows the priority
    **Gate 2 > Gate 3 > Gate 1** - entity ambiguity is the most trust-damaging,
    then inferred evidence, then insufficient history.

    The returned GateResult also exposes ``run_count`` - the live primary-signal
    run count Gate 1 observed - so T6 can persist gate_run_count as exactly the
    value seen here. The function never writes to the database.

    Parameters
    ----------
    opp
        The causal hypothesis under evaluation; provides ``cause_chain`` (Gate 3)
        and ``evidence_links`` (Gate 2). dict or object form accepted.
    signal_key
        The opportunity's primary signal_key, used for the Gate 1 run-count read.
    opportunity_id
        Opportunity identifier, used only for log context.
    enrichment
        The OppEnrichment; ``enrichment.entities`` feed Gate 2 alongside
        evidence_links.
    causal_context
        The CausalContext from build_causal_context(); its entities supply org_id.
    """

    org_id = _resolve_org_id(opp, enrichment, causal_context)

    # Gate 1 - sufficient temporal history. Read the LIVE count from
    # signal_snapshots. A read failure degrades to 0, which fails the gate - the
    # safe, preliminary-by-default outcome. run_count is threaded out regardless
    # of pass/fail so T6 can record gate_run_count as exactly this value.
    try:
        run_count = _primary_signal_run_count(org_id, signal_key)
    except Exception as exc:
        logger.warning("Gate 1 run_count read failed for opp=%s: %s", opportunity_id, exc)
        run_count = 0
    gate1_failed = run_count < GATE1_MIN_RUN_COUNT
    gate1_reason = (
        f"gate1_insufficient_run_count: {run_count} of {GATE1_MIN_RUN_COUNT} runs completed"
        if gate1_failed
        else None
    )

    # Gate 2 - resolved entity chain.
    try:
        unresolved_count = _gate2_unresolved_count(opp, enrichment, org_id)
    except Exception as exc:
        logger.warning("Gate 2 evaluation failed for opp=%s: %s", opportunity_id, exc)
        unresolved_count = 1  # conservative: cannot confirm resolution
    gate2_failed = unresolved_count > 0
    gate2_reason = (
        f"gate2_unresolved_entities: {unresolved_count} entities require resolution"
        if gate2_failed
        else None
    )

    # Gate 3 - directly observed cause chain.
    inferred_step_index = _gate3_inferred_step_index(opp, enrichment)
    gate3_failed = inferred_step_index is not None
    gate3_reason = (
        f"gate3_inferred_primary_step: step {inferred_step_index}"
        if gate3_failed
        else None
    )

    # Surfaced reason follows the priority Gate 2 > Gate 3 > Gate 1.
    if gate2_failed:
        return GateResult(True, gate2_reason, run_count)
    if gate3_failed:
        return GateResult(True, gate3_reason, run_count)
    if gate1_failed:
        return GateResult(True, gate1_reason, run_count)
    return GateResult(False, None, run_count)


# ---------------------------------------------------------------------------
# T6 - persist a validated, gate-evaluated hypothesis (ENT-6 Sections 4 & 5)
# ---------------------------------------------------------------------------
#
# store_causal_hypothesis() writes a hypothesis that has already passed T4
# (falsifiability / hallucination) and been scored by T5's gates. Its only job
# is to write the row accurately and completely, then emit the success
# telemetry. It is the sole writer of the causal_hypotheses table.

# Columns in causal_hypotheses, in DDL order (see
# database/models/causal_hypotheses.py). CausalHypothesis.to_db_row() returns
# exactly these keys.
_CAUSAL_HYPOTHESES_COLUMNS: tuple[str, ...] = (
    "id",
    "org_id",
    "opportunity_id",
    "run_id",
    "cause_chain",
    "evidence_links",
    "temporal_support",
    "confidence",
    "inferred",
    "falsifiability_condition",
    "preliminary",
    "preliminary_reason",
    "gate_run_count",
    "generated_by",
    "created_at",
)

# Confidence composite bounds and weights. The story leaves the weights open;
# these are an engineering default chosen so the score is deterministic (stable
# contract tests) and so even a weak-but-validated hypothesis scores >= 0.5.
_CONFIDENCE_FLOOR = 0.5
_CONFIDENCE_CEIL = 1.0
_CONFIDENCE_W_TEMPORAL = 0.40   # data maturity is the strongest trust signal
_CONFIDENCE_W_CORROBORATION = 0.35  # cross-system agreement
_CONFIDENCE_W_DEPTH = 0.25       # graph proximity of cause to effect


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _evidence_links_from_context(causal_context: Any) -> list[str]:
    """Derive evidence_links (supporting entity ids) from the causal context.

    parse_causal_output() (T4) returns only cause_chain + falsifiability_condition,
    so the grounding entity ids come from the assembled graph neighbourhood. Sorted
    for a deterministic stored value (stable contract tests).
    """

    graph = getattr(causal_context, "graph_context", None)
    entities = getattr(graph, "entities", []) if graph is not None else []
    ids = {getattr(entity, "entity_id", None) for entity in entities}
    return sorted(eid for eid in ids if eid)


def _distinct_source_systems(org_id: str, entity_ids: list[str]) -> list[str]:
    """Distinct source_systems behind the evidence entities (corroboration input).

    Best-effort: a read failure returns [] so a transient hiccup lowers the
    confidence score rather than losing the hypothesis.
    """

    if not entity_ids:
        return []
    try:
        conn = db.connect()
        try:
            cur = conn.cursor()
            placeholders = ", ".join("%s" for _ in entity_ids)
            cur.execute(
                f"SELECT DISTINCT source_system FROM entities "
                f"WHERE org_id = %s AND id IN ({placeholders})",
                (org_id, *entity_ids),
            )
            rows = cur.fetchall()
            return sorted({row["source_system"] for row in rows if row["source_system"]})
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("source_system lookup failed for corroboration: %s", exc)
        return []


def _temporal_entry_for_entity(
    temporal_support: dict[str, Any], entity: Any
) -> Optional[dict[str, Any]]:
    """Find temporal_support for an entity by detector/display id or UUID.

    signal_key format is ``{pack_id}::{detector_id}::metric_value`` in the
    signal snapshot table. Older tests and some callers still use the entity
    UUID as the middle segment, so both are accepted.
    """

    candidates = {
        str(value).strip()
        for value in (
            getattr(entity, "display_name", None),
            getattr(entity, "entity_id", None),
        )
        if value and str(value).strip()
    }
    for signal_key, entry in temporal_support.items():
        parts = str(signal_key).split("::")
        if len(parts) >= 2 and parts[1] in candidates:
            return entry
    return None


def compute_causal_confidence(causal_context: Any, source_systems: list[str]) -> float:
    """Composite confidence in [0.5, 1.0] for a stored hypothesis (read by T3-S17-A).

    Three factors, each normalised to [0, 1] then combined as a weighted average
    and mapped onto [0.5, 1.0]. Deterministic given the same inputs.

    1. Graph depth - fewer hops connecting cause to effect score higher. Measured
       from the dependency_paths assembled in build_causal_context(); a direct
       edge (1 hop) scores 1.0, a depth-3 path scores ~0.33. No measured path is
       neutral (0.5).
    2. Temporal support - the fraction of the chain's process entities whose
       signal history is mature (run_count >= 10, the Gate 1 bar).
    3. Corroboration - the number of distinct source_systems contributing
       evidence (Salesforce + ServiceNow + Jira = strong), saturating at 3.
    """

    # Factor 1 - graph depth.
    paths = getattr(causal_context, "dependency_paths", None) or []
    if paths:
        avg_hops = sum(max(len(path) - 1, 0) for path in paths) / len(paths)
        depth_score = _clamp(1.0 - (avg_hops - 1.0) / 3.0, 0.0, 1.0)
    else:
        depth_score = 0.5

    # Factor 2 - temporal support maturity.
    graph = getattr(causal_context, "graph_context", None)
    entities = getattr(graph, "entities", []) if graph is not None else []
    process_entities = [
        entity for entity in entities if getattr(entity, "entity_type", None) == "process"
    ]
    temporal_support = getattr(causal_context, "temporal_support", None) or {}
    if process_entities:
        mature = 0
        for entity in process_entities:
            entry = _temporal_entry_for_entity(temporal_support, entity)
            if entry and int(entry.get("run_count", 0) or 0) >= GATE1_MIN_RUN_COUNT:
                mature += 1
        temporal_score = mature / len(process_entities)
    else:
        temporal_score = 0.0

    # Factor 3 - corroboration breadth.
    corroboration_score = _clamp(len(source_systems) / 3.0, 0.0, 1.0)

    raw = (
        _CONFIDENCE_W_TEMPORAL * temporal_score
        + _CONFIDENCE_W_CORROBORATION * corroboration_score
        + _CONFIDENCE_W_DEPTH * depth_score
    )
    confidence = _CONFIDENCE_FLOOR + (_CONFIDENCE_CEIL - _CONFIDENCE_FLOOR) * raw
    return round(_clamp(confidence, _CONFIDENCE_FLOOR, _CONFIDENCE_CEIL), 4)


def _gate_field(gate_result: Any, attr: str, index: Optional[int], default: Any = None) -> Any:
    """Read a field from a GateResult, tolerating a plain (preliminary, reason) tuple."""

    if hasattr(gate_result, attr):
        return getattr(gate_result, attr)
    if index is not None:
        try:
            return gate_result[index]
        except (TypeError, IndexError, KeyError):
            return default
    return default


def _insert_causal_hypothesis(row: dict[str, Any]) -> None:
    """Parameterised single-row INSERT into causal_hypotheses (commit on success)."""

    placeholders = ", ".join("%s" for _ in _CAUSAL_HYPOTHESES_COLUMNS)
    sql = (
        f"INSERT INTO causal_hypotheses ({', '.join(_CAUSAL_HYPOTHESES_COLUMNS)}) "
        f"VALUES ({placeholders})"
    )
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(row[column] for column in _CAUSAL_HYPOTHESES_COLUMNS))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _emit_hypothesis_generated(
    *,
    org_id: str,
    run_id: str,
    opportunity_id: str,
    preliminary: bool,
    confidence: float,
    gate_run_count: int,
    inferred: bool,
) -> None:
    """Fire causal.hypothesis_generated. Called only after a successful commit.

    Payload matches CausalHypothesisGeneratedPayload (T10): identifiers, the
    preliminary flag, and gate metrics only - never hypothesis text (PII guard).
    """

    try:
        from app.telemetry import record_event
    except ModuleNotFoundError:  # pragma: no cover
        from backend.app.telemetry import record_event  # type: ignore[no-redef]

    record_event(
        "causal.hypothesis_generated",
        {
            "org_id": org_id,
            "run_id": run_id,
            "opportunity_id": opportunity_id,
            "preliminary": bool(preliminary),
            "confidence": float(confidence),
            "gate_run_count": int(gate_run_count),
            "inferred": bool(inferred),
        },
    )


def store_causal_hypothesis(
    org_id: str,
    opportunity_id: str,
    run_id: str,
    parsed_output: Any,
    gate_result: Any,
    causal_context: Any,
) -> Optional[str]:
    """Persist a validated, gate-evaluated causal hypothesis. Returns the new row id.

    By the time this runs the hypothesis has passed T4 (falsifiability /
    hallucination) and been scored by T5's gates; storage writes the result
    accurately and completely, then emits the success telemetry.

    Column population (all 15; see Section 4):
      * cause_chain / falsifiability_condition - from ``parsed_output``.
      * evidence_links - the grounding entity ids from ``causal_context``.
      * temporal_support - ``causal_context.temporal_support`` (or None).
      * preliminary / preliminary_reason - straight from ``gate_result``
        (reason is None only on a full pass).
      * gate_run_count - the live Gate 1 count threaded out of T5
        (``gate_result.run_count``); never re-estimated.
      * inferred - True when any cause_chain step carries the ``[inferred:``
        label, using the SAME detector as Gate 3 (cause_chain_uses_inferred) so
        the column and the gate never disagree. inferred is permanent metadata
        and is distinct from preliminary: a row can have preliminary=False and
        inferred=True if inferred steps exist but none is a step's primary
        evidence.
      * confidence - the deterministic composite of compute_causal_confidence().
      * generated_by - 'llm'; created_at - current UTC ISO timestamp.

    Graceful degradation (per CLAUDE.md): the write is wrapped so a failure is
    logged, emits no telemetry, and returns None rather than crashing the run.
    causal.hypothesis_generated fires only after the row commits.
    """

    cause_chain = [str(step) for step in (_get(parsed_output, "cause_chain") or [])]
    falsifiability_condition = str(_get(parsed_output, "falsifiability_condition") or "")

    preliminary = bool(_gate_field(gate_result, "preliminary", 0, default=True))
    preliminary_reason = _gate_field(gate_result, "reason", 1, default=None)
    # Invariant (Section 4): preliminary_reason is null only on a full pass.
    preliminary_reason = preliminary_reason if preliminary else None
    gate_run_count = int(getattr(gate_result, "run_count", 0) or 0)

    evidence_links = _evidence_links_from_context(causal_context)
    temporal_support = getattr(causal_context, "temporal_support", None)
    # Same detection as Gate 3 - the column and the gate can never disagree.
    inferred = cause_chain_uses_inferred(cause_chain)

    try:
        source_systems = _distinct_source_systems(org_id, evidence_links)
        confidence = compute_causal_confidence(causal_context, source_systems)
        hypothesis = CausalHypothesis(
            org_id=org_id,
            opportunity_id=opportunity_id,
            run_id=run_id,
            cause_chain=cause_chain,
            evidence_links=evidence_links,
            confidence=confidence,
            inferred=inferred,
            falsifiability_condition=falsifiability_condition,
            preliminary=preliminary,
            gate_run_count=gate_run_count,
            generated_by="llm",
            temporal_support=temporal_support,
            preliminary_reason=preliminary_reason,
        )
        row = hypothesis.to_db_row()
        _insert_causal_hypothesis(row)
    except Exception as exc:
        # Degrade gracefully: no row, no event, run continues.
        logger.error(
            "store_causal_hypothesis: failed to persist hypothesis for opp=%s run=%s: %s",
            opportunity_id,
            run_id,
            exc,
        )
        return None

    # Success: emit telemetry only now that the write has committed. A telemetry
    # hiccup must not undo a successful store, so it is non-blocking.
    try:
        _emit_hypothesis_generated(
            org_id=org_id,
            run_id=run_id,
            opportunity_id=opportunity_id,
            preliminary=preliminary,
            confidence=confidence,
            gate_run_count=gate_run_count,
            inferred=inferred,
        )
    except Exception as exc:
        logger.warning(
            "causal.hypothesis_generated telemetry failed (non-blocking) for opp=%s: %s",
            opportunity_id,
            exc,
        )

    return row["id"]
