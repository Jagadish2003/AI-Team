"""Contract tests for ENT-4 / T3-S14-A — graph.context_built telemetry (T7).

Focused verification of the telemetry emitted by
``app.graph_context_builder.build_graph_context()``:

  AC10 — graph.context_built telemetry event is fired after every
         build_graph_context() and includes entity_count, entity_count_shown,
         truncated, and duration_ms.
  T7   — graph.context_built is registered in the telemetry registry with a
         payload schema, so record_event() accepts it (does not raise) and the
         event persists / logs.

These tests deliberately exercise the telemetry contract directly:
  * registration in REGISTERED_EVENT_TYPES / EVENT_PAYLOAD_TYPES,
  * the GraphContextBuiltPayload field set,
  * that record_event("graph.context_built", ...) does not raise (registered)
    while an unregistered type does (contrast — proves the registration matters),
  * that build_graph_context fires exactly one event on every path
    (normal / sparse / truncated) with correct payload values,
  * that the real (unpatched) telemetry path logs the event (caplog), and
  * that a telemetry failure never breaks the build.
"""
import logging
import os
import sqlite3
import uuid

import pytest

from app import graph_context_builder as gcb
from app.graph_context_builder import GRAPH_CONTEXT_MAX_ENTITIES, build_graph_context
from app.telemetry import (
    EVENT_PAYLOAD_TYPES,
    REGISTERED_EVENT_TYPES,
    GraphContextBuiltPayload,
    record_event,
)
from database.models.entities import Entity
from database.models.entity_relationships import OBSERVED_CONFIDENCE, EntityRelationship

EVENT = "graph.context_built"
REQUIRED_PAYLOAD_FIELDS = ("entity_count", "entity_count_shown", "truncated", "duration_ms")


# ---------------------------------------------------------------------------
# Seeding helpers (SQLite contract DB)
# ---------------------------------------------------------------------------

def _insert_entity(org_id, display_name, *, entity_type="person", resolution_status="resolved",
                   run_count=1, run_id="run_tel"):
    entity = Entity(
        org_id=org_id, entity_type=entity_type,
        canonical_name=" ".join(display_name.split()).lower() + "-" + uuid.uuid4().hex[:8],
        display_name=display_name, source_system="test", resolution_confidence=1.0,
        resolution_status=resolution_status, first_seen_run_id=run_id,
        last_seen_run_id=run_id, run_count=run_count,
    )
    row = entity.to_db_row()
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name, source_system,
                source_record_id, resolution_confidence, resolution_status,
                first_seen_run_id, last_seen_run_id, run_count, metadata, created_at, updated_at
            ) VALUES (
                :id, :org_id, :entity_type, :canonical_name, :display_name, :source_system,
                :source_record_id, :resolution_confidence, :resolution_status,
                :first_seen_run_id, :last_seen_run_id, :run_count, :metadata, :created_at, :updated_at
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _insert_relationship(org_id, from_id, to_id, *, relationship_type="owns", run_id="run_tel"):
    rel = EntityRelationship(
        org_id=org_id, from_entity_id=from_id, to_entity_id=to_id,
        relationship_type=relationship_type, confidence=OBSERVED_CONFIDENCE, inferred=False,
        first_seen_run_id=run_id, last_seen_run_id=run_id, run_count=1,
    )
    row = rel.to_db_row()
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO entity_relationships (
                id, org_id, from_entity_id, to_entity_id, relationship_type,
                confidence, inferred, evidence, first_seen_run_id, last_seen_run_id,
                run_count, created_at
            ) VALUES (
                :id, :org_id, :from_entity_id, :to_entity_id, :relationship_type,
                :confidence, :inferred, :evidence, :first_seen_run_id, :last_seen_run_id,
                :run_count, :created_at
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _org():
    return "org_" + uuid.uuid4().hex[:10]


def _chain(org, names):
    ids = [_insert_entity(org, n) for n in names]
    for a, b in zip(ids, ids[1:]):
        _insert_relationship(org, a, b)
    return ids


def _capture(monkeypatch):
    captured = []
    monkeypatch.setattr(
        gcb, "record_event",
        lambda event_type, payload=None: captured.append((event_type, payload or {})),
    )
    return captured


# ---------------------------------------------------------------------------
# T7 — registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_event_registered(self):
        assert EVENT in REGISTERED_EVENT_TYPES

    def test_event_has_payload_schema(self):
        assert EVENT in EVENT_PAYLOAD_TYPES
        assert EVENT_PAYLOAD_TYPES[EVENT] is GraphContextBuiltPayload

    def test_payload_schema_has_required_fields(self):
        annotations = GraphContextBuiltPayload.__annotations__
        for field in REQUIRED_PAYLOAD_FIELDS:
            assert field in annotations, f"{field} missing from GraphContextBuiltPayload"

    def test_record_event_accepts_registered_type(self):
        # Registered -> must NOT raise ValueError.
        record_event(EVENT, {
            "org_id": "o", "opportunity_id": "opp",
            "entity_count": 0, "entity_count_shown": 0,
            "truncated": False, "duration_ms": 1,
        })

    def test_record_event_rejects_unregistered_type(self):
        # Contrast: an unregistered type DOES raise — proving registration is required.
        with pytest.raises(ValueError):
            record_event("graph.not_a_real_event", {})

    def test_payload_exported(self):
        import app.telemetry as telemetry
        assert "GraphContextBuiltPayload" in telemetry.__all__


# ---------------------------------------------------------------------------
# AC10 — fired every build with correct payload
# ---------------------------------------------------------------------------

class TestFiresEveryBuild:
    def test_fires_exactly_once_on_normal_build(self, monkeypatch):
        captured = _capture(monkeypatch)
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        build_graph_context(org, "opp1", [a], max_depth=2)
        events = [e for e in captured if e[0] == EVENT]
        assert len(events) == 1

    def test_payload_has_all_required_fields(self, monkeypatch):
        captured = _capture(monkeypatch)
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        build_graph_context(org, "opp2", [a], max_depth=2)
        payload = [e for e in captured if e[0] == EVENT][0][1]
        for field in REQUIRED_PAYLOAD_FIELDS:
            assert field in payload, f"{field} missing from emitted payload"

    def test_payload_values_correct(self, monkeypatch):
        captured = _capture(monkeypatch)
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        build_graph_context(org, "opp3", [a], max_depth=2)
        payload = [e for e in captured if e[0] == EVENT][0][1]
        assert payload["entity_count"] == 3
        assert payload["entity_count_shown"] == 3
        assert payload["truncated"] is False
        assert isinstance(payload["duration_ms"], int)
        assert payload["duration_ms"] >= 0
        assert payload["opportunity_id"] == "opp3"

    def test_fires_on_sparse_build(self, monkeypatch):
        captured = _capture(monkeypatch)
        org = _org()
        a = _insert_entity(org, "Solo")
        build_graph_context(org, "opp_sparse", [a], max_depth=2)
        events = [e for e in captured if e[0] == EVENT]
        assert len(events) == 1
        assert events[0][1]["entity_count"] == 1
        # all four required fields present even on the sparse path
        for field in REQUIRED_PAYLOAD_FIELDS:
            assert field in events[0][1]

    def test_fires_on_empty_build(self, monkeypatch):
        captured = _capture(monkeypatch)
        org = _org()
        build_graph_context(org, "opp_empty", [], max_depth=2)
        events = [e for e in captured if e[0] == EVENT]
        assert len(events) == 1
        assert events[0][1]["entity_count"] == 0

    def test_truncated_true_in_payload_when_capped(self, monkeypatch):
        captured = _capture(monkeypatch)
        org = _org()
        hub = _insert_entity(org, "Hub", run_count=100)
        for i in range(17):  # 1 hub + 17 leaves = 18 > 15
            leaf = _insert_entity(org, f"Leaf{i:02d}", entity_type="object", run_count=i)
            _insert_relationship(org, hub, leaf)
        build_graph_context(org, "opp_trunc", [hub], max_depth=1)
        payload = [e for e in captured if e[0] == EVENT][0][1]
        assert payload["entity_count"] == 18
        assert payload["entity_count_shown"] == GRAPH_CONTEXT_MAX_ENTITIES == 15
        assert payload["truncated"] is True

    def test_entity_count_shown_never_exceeds_cap(self, monkeypatch):
        captured = _capture(monkeypatch)
        org = _org()
        hub = _insert_entity(org, "Hub")
        for i in range(25):
            leaf = _insert_entity(org, f"L{i:02d}", entity_type="object")
            _insert_relationship(org, hub, leaf)
        build_graph_context(org, "opp_big", [hub], max_depth=1)
        payload = [e for e in captured if e[0] == EVENT][0][1]
        assert payload["entity_count_shown"] <= GRAPH_CONTEXT_MAX_ENTITIES


# ---------------------------------------------------------------------------
# Real (unpatched) telemetry path — registration lets the event log/persist
# ---------------------------------------------------------------------------

class TestRealTelemetryPath:
    def test_record_event_logs_graph_context_built(self, caplog):
        # record_event logs every event via logger.info (the documented caplog
        # observation point). A registered event reaches this log; this confirms
        # the real telemetry path accepts and records the event.
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            record_event(EVENT, {
                "org_id": "o", "opportunity_id": "opp",
                "entity_count": 3, "entity_count_shown": 3,
                "truncated": False, "duration_ms": 2,
            })
        assert EVENT in caplog.text

    def test_real_build_does_not_raise(self):
        # Unpatched record_event: registered event type means no ValueError,
        # and the build returns a valid context.
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        ctx = build_graph_context(org, "opp_real", [a], max_depth=2)
        assert ctx.entity_count == 3


# ---------------------------------------------------------------------------
# Non-blocking guarantee
# ---------------------------------------------------------------------------

class TestNonBlocking:
    def test_telemetry_failure_does_not_break_build(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("telemetry down")
        monkeypatch.setattr(gcb, "record_event", boom)
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        ctx = build_graph_context(org, "opp_boom", [a], max_depth=2)  # must not raise
        assert ctx.entity_count == 3


# ---------------------------------------------------------------------------
# Call-site uses the registered literal
# ---------------------------------------------------------------------------

class TestCallSite:
    def test_build_graph_context_uses_registered_event_literal(self):
        import inspect
        from app import graph_context_builder
        src = inspect.getsource(graph_context_builder.build_graph_context)
        assert '"graph.context_built"' in src or "'graph.context_built'" in src
