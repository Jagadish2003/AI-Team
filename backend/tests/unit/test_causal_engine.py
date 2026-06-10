"""Unit tests for causal_engine.py — T2 (ENT-6/T3-S16-A).

Covers:
  - CausalContext fields populated correctly from graph + temporal data.
  - InsufficientGraphContextError raised when neighbourhood < 3 entities.
  - Inferred edges included in depth-3 traversal.
  - Process entities with run_count < 5 excluded from temporal support.
  - Dependency paths computed between process entity pairs.

No live credentials required — all infrastructure calls are monkeypatched.
"""
from __future__ import annotations

import pytest

from app.causal_engine import (
    CausalContext,
    EdgeNode,
    EntityNode,
    GraphNeighbourhood,
    InsufficientGraphContextError,
    build_causal_context,
    _depth3_neighbourhood,
    _shortest_path,
    _build_temporal_support,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_entity(
    entity_id: str,
    entity_type: str = "process",
    display_name: str = "",
    resolution_status: str = "resolved",
    org_id: str = "test_org",
) -> EntityNode:
    return EntityNode(
        entity_id=entity_id,
        entity_type=entity_type,
        display_name=display_name or entity_id,
        resolution_status=resolution_status,
        org_id=org_id,
    )


def _make_edge(fid: str, tid: str, rtype: str = "depends_on", inferred: bool = False) -> EdgeNode:
    return EdgeNode(
        from_entity_id=fid,
        to_entity_id=tid,
        relationship_type=rtype,
        inferred=inferred,
        confidence=0.9 if not inferred else 0.6,
    )


def _neighbourhood(*entity_ids: str, inferred: bool = False) -> GraphNeighbourhood:
    """Build a simple chain neighbourhood: e0 → e1 → e2 → ..."""
    ids = list(entity_ids)
    entities = [_make_entity(eid) for eid in ids]
    edges = [
        _make_edge(ids[i], ids[i + 1], inferred=inferred)
        for i in range(len(ids) - 1)
    ]
    return GraphNeighbourhood(entities=entities, edges=edges)


# ---------------------------------------------------------------------------
# _shortest_path tests
# ---------------------------------------------------------------------------

def test_shortest_path_direct_edge():
    nb = _neighbourhood("A", "B", "C")
    path = _shortest_path(nb, "A", "C")
    assert path is not None
    assert path[0] == "A"
    assert path[-1] == "C"


def test_shortest_path_same_node():
    nb = _neighbourhood("A", "B")
    assert _shortest_path(nb, "A", "A") == ["A"]


def test_shortest_path_unreachable():
    nb = _neighbourhood("A", "B")
    # C is not in the neighbourhood at all
    assert _shortest_path(nb, "A", "C") is None


# ---------------------------------------------------------------------------
# _depth3_neighbourhood tests (monkeypatched DB)
# ---------------------------------------------------------------------------

def _raw_edge_row(fid: str, tid: str, inferred: bool = False) -> dict:
    return {
        "from_entity_id": fid,
        "to_entity_id": tid,
        "relationship_type": "depends_on",
        "inferred": 1 if inferred else 0,
        "confidence": 0.6 if inferred else 0.9,
        "from_entity_type": "process",
        "from_display_name": fid,
        "from_resolution_status": "resolved",
        "to_entity_type": "process",
        "to_display_name": tid,
        "to_resolution_status": "resolved",
    }


def test_depth3_neighbourhood_includes_inferred(monkeypatch):
    """Inferred edges must be included in the depth-3 traversal."""
    call_log: list[tuple[str, str]] = []

    def fake_raw_edges(org_id: str, entity_id: str) -> list[dict]:
        call_log.append((org_id, entity_id))
        if entity_id == "seed":
            return [_raw_edge_row("seed", "child", inferred=True)]
        if entity_id == "child":
            return [_raw_edge_row("child", "grandchild", inferred=False)]
        return []

    monkeypatch.setattr(
        "app.causal_engine._raw_edges_for_entity", fake_raw_edges
    )

    nb = _depth3_neighbourhood("org1", ["seed"])
    entity_ids = {e.entity_id for e in nb.entities}
    assert "seed" in entity_ids
    assert "child" in entity_ids
    assert "grandchild" in entity_ids

    inferred_edges = [e for e in nb.edges if e.inferred]
    assert len(inferred_edges) == 1
    assert inferred_edges[0].from_entity_id == "seed"
    assert inferred_edges[0].to_entity_id == "child"


def test_depth3_neighbourhood_stops_at_depth3(monkeypatch):
    """Traversal must not go beyond depth 3."""
    def fake_raw_edges(org_id: str, entity_id: str) -> list[dict]:
        # Build a chain: seed → d1 → d2 → d3 → d4
        chain = {"seed": "d1", "d1": "d2", "d2": "d3", "d3": "d4"}
        if entity_id in chain:
            return [_raw_edge_row(entity_id, chain[entity_id])]
        return []

    monkeypatch.setattr("app.causal_engine._raw_edges_for_entity", fake_raw_edges)

    nb = _depth3_neighbourhood("org1", ["seed"])
    entity_ids = {e.entity_id for e in nb.entities}
    # seed(0) → d1(1) → d2(2) → d3(3): d4 is at depth 4, must be absent
    assert "seed" in entity_ids
    assert "d3" in entity_ids
    assert "d4" not in entity_ids


# ---------------------------------------------------------------------------
# _build_temporal_support tests
# ---------------------------------------------------------------------------

class _FakeTrend:
    def __init__(self, run_count: int, trend_direction: str = "rising", signal_key: str = ""):
        self.run_count = run_count
        self.trend_direction = trend_direction
        self.signal_key = signal_key


class _FakeAnomaly:
    def __init__(self, is_anomalous: bool = False, insufficient_data: bool = False,
                 first_deviation: bool = False, baseline_mean: float = 1.0,
                 baseline_stddev: float = 0.1, signal_key: str = ""):
        self.is_anomalous = is_anomalous
        self.insufficient_data = insufficient_data
        self.first_deviation = first_deviation
        self.baseline_mean = baseline_mean
        self.baseline_stddev = baseline_stddev
        self.signal_key = signal_key
        self.anomaly_direction = None


def test_temporal_support_excludes_low_run_count(monkeypatch):
    """Signals with run_count < 5 must be excluded from temporal_support."""
    monkeypatch.setattr(
        "app.causal_engine.calculate_trend",
        lambda org_id, sk: _FakeTrend(run_count=4, signal_key=sk),
    )
    entities = [_make_entity("e1", entity_type="process")]
    result = _build_temporal_support("org1", "svc", entities)
    assert result == {}


def test_temporal_support_includes_sufficient_run_count(monkeypatch):
    """Signals with run_count >= 5 must appear in temporal_support."""
    monkeypatch.setattr(
        "app.causal_engine.calculate_trend",
        lambda org_id, sk: _FakeTrend(run_count=7, trend_direction="rising", signal_key=sk),
    )
    monkeypatch.setattr(
        "app.causal_engine.calculate_anomaly",
        lambda org_id, sk, current_value: _FakeAnomaly(signal_key=sk),
    )
    monkeypatch.setattr(
        "app.causal_engine.build_baseline_context",
        lambda trend, anomaly, current_value: "Trending up",
    )
    entities = [_make_entity("e1", entity_type="process")]
    result = _build_temporal_support("org1", "svc", entities)
    key = "svc::e1::metric_value"
    assert key in result
    assert result[key]["trend"] == "rising"
    assert result[key]["run_count"] == 7
    assert result[key]["context"] == "Trending up"


# ---------------------------------------------------------------------------
# build_causal_context integration tests
# ---------------------------------------------------------------------------

def _setup_rich_graph(monkeypatch, org_id: str = "org1") -> None:
    """Patch _raw_edges_for_entity so a 4-entity process graph is returned."""
    # e1 (process) — e2 (process) — e3 (process) — e4 (person)
    graph = {
        "e1": [_raw_edge_row("e1", "e2"), _raw_edge_row("e1", "e4")],
        "e2": [_raw_edge_row("e1", "e2"), _raw_edge_row("e2", "e3")],
        "e3": [_raw_edge_row("e2", "e3")],
        "e4": [_raw_edge_row("e1", "e4")],
    }
    # e4 is person type
    graph["e4"][0]["from_entity_type"] = "process"
    graph["e4"][0]["to_entity_type"] = "person"
    graph["e1"][1]["to_entity_type"] = "person"

    def fake_raw_edges(org_id_: str, entity_id: str) -> list[dict]:
        return list(graph.get(entity_id, []))

    monkeypatch.setattr("app.causal_engine._raw_edges_for_entity", fake_raw_edges)


def test_build_causal_context_populated(monkeypatch):
    """build_causal_context returns a CausalContext with all three fields."""
    _setup_rich_graph(monkeypatch)
    monkeypatch.setattr(
        "app.causal_engine.calculate_trend",
        lambda org_id, sk: _FakeTrend(run_count=6, trend_direction="stable", signal_key=sk),
    )
    monkeypatch.setattr(
        "app.causal_engine.calculate_anomaly",
        lambda org_id, sk, current_value: _FakeAnomaly(signal_key=sk),
    )
    monkeypatch.setattr(
        "app.causal_engine.build_baseline_context",
        lambda trend, anomaly, current_value: "Stable",
    )

    ctx = build_causal_context("org1", "opp-1", ["e1"], "svc")

    assert isinstance(ctx, CausalContext)
    assert ctx.graph_context.entity_count >= 3
    assert isinstance(ctx.dependency_paths, list)
    assert isinstance(ctx.temporal_support, dict)


def test_build_causal_context_sparse_raises(monkeypatch):
    """InsufficientGraphContextError raised when entity count < 3 (AC9)."""
    def fake_raw_edges(org_id_: str, entity_id: str) -> list[dict]:
        # Only one hop from seed — total 2 entities
        if entity_id == "seed":
            return [_raw_edge_row("seed", "only_one")]
        return []

    monkeypatch.setattr("app.causal_engine._raw_edges_for_entity", fake_raw_edges)

    with pytest.raises(InsufficientGraphContextError):
        build_causal_context("org1", "opp-sparse", ["seed"], "svc")


def test_build_causal_context_inferred_edges_present(monkeypatch):
    """Inferred edges from the neighbourhood are preserved in graph_context."""
    def fake_raw_edges(org_id_: str, entity_id: str) -> list[dict]:
        if entity_id == "e1":
            return [
                _raw_edge_row("e1", "e2", inferred=True),
                _raw_edge_row("e1", "e3", inferred=False),
            ]
        if entity_id == "e2":
            return [_raw_edge_row("e1", "e2", inferred=True)]
        if entity_id == "e3":
            return [_raw_edge_row("e1", "e3", inferred=False)]
        return []

    monkeypatch.setattr("app.causal_engine._raw_edges_for_entity", fake_raw_edges)
    monkeypatch.setattr(
        "app.causal_engine.calculate_trend",
        lambda org_id, sk: _FakeTrend(run_count=3, signal_key=sk),
    )

    ctx = build_causal_context("org1", "opp-1", ["e1"], "svc")
    inferred = [e for e in ctx.graph_context.edges if e.inferred]
    assert len(inferred) >= 1


def test_build_causal_context_only_process_entities_get_temporal(monkeypatch):
    """Only entities with entity_type='process' are included in temporal support."""
    def fake_raw_edges(org_id_: str, entity_id: str) -> list[dict]:
        if entity_id == "proc1":
            row1 = _raw_edge_row("proc1", "person1")
            row1["to_entity_type"] = "person"
            row2 = _raw_edge_row("proc1", "proc2")
            return [row1, row2]
        if entity_id == "person1":
            r = _raw_edge_row("proc1", "person1")
            r["to_entity_type"] = "person"
            return [r]
        if entity_id == "proc2":
            return [_raw_edge_row("proc1", "proc2")]
        return []

    monkeypatch.setattr("app.causal_engine._raw_edges_for_entity", fake_raw_edges)

    called_signal_keys: list[str] = []

    def fake_trend(org_id: str, signal_key: str):
        called_signal_keys.append(signal_key)
        return _FakeTrend(run_count=6, signal_key=signal_key)

    monkeypatch.setattr("app.causal_engine.calculate_trend", fake_trend)
    monkeypatch.setattr(
        "app.causal_engine.calculate_anomaly",
        lambda org_id, sk, current_value: _FakeAnomaly(signal_key=sk),
    )
    monkeypatch.setattr(
        "app.causal_engine.build_baseline_context",
        lambda trend, anomaly, current_value: "ok",
    )

    build_causal_context("org1", "opp-1", ["proc1"], "svc")

    # person1 entity must not appear in any signal_key query
    for sk in called_signal_keys:
        assert "person1" not in sk
