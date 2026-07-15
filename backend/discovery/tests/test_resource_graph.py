"""MSP-B0 / AT-639 — tests for conservative resource-entity creation.

Covers:
  * T5-AC1 — referenced resources become graph entities.
  * T5-AC2 — resource entities are created only when supported by observed events.
  * T5-AC3 — no speculative estate modeling is introduced.
  * T5-AC4 — graph entities support downstream discovery components.

The core entity resolver hits the database; these unit tests inject a fake
resolver that records its calls and returns a real ``Entity`` built from the
kwargs, so the event-processing contract is verified without a DB.
"""
import json
import os

from database.models.entities import Entity
from discovery.signals import (
    CLOUD_RESOURCE_ENTITY_TYPE,
    MAPPERS,
    OperationalEvent,
    ResourceRef,
    create_resource_entities,
)

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "msp_provider_mapping_golden.json")


class _FakeResolver:
    """Records resolve_or_create_entity calls; returns a real resolved Entity."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return Entity(
            org_id=kwargs["org_id"],
            entity_type=kwargs["entity_type"],
            canonical_name=" ".join(kwargs["display_name"].split()).lower(),
            display_name=kwargs["display_name"],
            source_system=kwargs["source_system"],
            source_record_id=kwargs.get("source_record_id"),
            resolution_confidence=1.0,        # stable system id → 1.0
            resolution_status="resolved",
            first_seen_run_id=kwargs["run_id"],
            last_seen_run_id=kwargs["run_id"],
            metadata=kwargs.get("metadata"),
        )


def _event(**over):
    base = dict(
        org_id="acme", source_system="aws", signal_id="s1",
        event_type="X", event_class="state_change", severity="info",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1"),
    )
    base.update(over)
    return OperationalEvent.build(**base)


# ── T5-AC1: referenced resources become entities ────────────────────────────

def test_referenced_resource_becomes_entity():
    r = _FakeResolver()
    events = [_event()]
    entities = create_resource_entities(events, run_id="run-1", resolver=r)
    assert len(entities) == 1
    assert len(r.calls) == 1
    call = r.calls[0]
    assert call["entity_type"] == CLOUD_RESOURCE_ENTITY_TYPE == "system"
    assert call["source_record_id"] == "i-1"
    assert call["display_name"] == "i-1"
    assert call["source_system"] == "aws"
    assert call["run_id"] == "run-1"
    assert call["metadata"]["cloud_resource"] is True
    assert call["metadata"]["resource_type"] == "compute"


# ── T5-AC2: created only when supported by observed events ──────────────────

def test_event_without_resource_creates_nothing():
    r = _FakeResolver()
    ev = _event(resource=None)
    entities = create_resource_entities([ev], run_id="run-1", resolver=r)
    assert entities == []
    assert r.calls == []


def test_only_referenced_resources_created_no_extras():
    r = _FakeResolver()
    events = [
        _event(signal_id="a", resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1")),
        _event(signal_id="b", resource=None),  # no resource → no entity
        _event(signal_id="c", resource=ResourceRef(provider="aws", resource_type="storage", resource_id="bucket-1")),
    ]
    entities = create_resource_entities(events, run_id="run-1", resolver=r)
    created_ids = {c["source_record_id"] for c in r.calls}
    # exactly the two referenced resources, nothing speculative
    assert created_ids == {"i-1", "bucket-1"}
    assert len(entities) == 2


# ── T5-AC3: no speculative estate modeling ──────────────────────────────────

def test_no_speculative_resources_beyond_referenced():
    r = _FakeResolver()
    # one event referencing one resource -> exactly one entity, no inferred
    # parents/children/siblings
    events = [_event(resource=ResourceRef(provider="azure", resource_type="compute",
                                          resource_id="/subscriptions/s/vm1"))]
    entities = create_resource_entities(events, run_id="run-1", resolver=r)
    assert len(entities) == 1
    assert len(r.calls) == 1


def test_duplicate_resource_across_events_resolved_once():
    r = _FakeResolver()
    ref = ResourceRef(provider="aws", resource_type="compute", resource_id="i-dup")
    events = [_event(signal_id="e1", resource=ref), _event(signal_id="e2", resource=ref)]
    create_resource_entities(events, run_id="run-1", resolver=r)
    assert len(r.calls) == 1  # de-duped per (org, resource_id)


# ── org scoping ─────────────────────────────────────────────────────────────

def test_org_scoping_same_resource_id_two_orgs():
    r = _FakeResolver()
    ref = ResourceRef(provider="aws", resource_type="compute", resource_id="i-shared")
    events = [
        _event(org_id="org-a", resource=ref),
        _event(org_id="org-b", resource=ref),
    ]
    create_resource_entities(events, run_id="run-1", resolver=r)
    orgs = {(c["org_id"], c["source_record_id"]) for c in r.calls}
    # one entity per org — resources never cross the org boundary
    assert orgs == {("org-a", "i-shared"), ("org-b", "i-shared")}


# ── T5-AC4: downstream-usable graph entities ────────────────────────────────

def test_returned_entities_are_valid_graph_entities():
    r = _FakeResolver()
    entities = create_resource_entities([_event()], run_id="run-1", resolver=r)
    e = entities[0]
    assert isinstance(e, Entity)
    assert e.entity_type == "system"
    assert e.org_id == "acme"
    assert e.source_record_id == "i-1"
    assert e.resolution_status == "resolved"
    assert e.resolution_confidence == 1.0
    # round-trips to a persistable DB row (what downstream graph consumers read)
    row = e.to_db_row()
    assert row["entity_type"] == "system"
    assert json.loads(row["metadata"])["cloud_resource"] is True


# ── integration: golden provider fixtures → resource entities ───────────────

def test_golden_fixture_events_promote_their_resources():
    with open(GOLDEN, "r", encoding="utf-8") as fh:
        cases = json.load(fh)["cases"]
    r = _FakeResolver()
    events = [MAPPERS[c["mapper"]](c["raw"], org_id=c["org_id"]) for c in cases]
    entities = create_resource_entities(events, run_id="run-1", resolver=r)
    # every golden case references a resource, so each distinct resource_id
    # becomes exactly one entity
    distinct_resource_ids = {ev.resource.resource_id for ev in events if ev.resource}
    created_ids = {c["source_record_id"] for c in r.calls}
    assert created_ids == distinct_resource_ids
    assert len(entities) == len(distinct_resource_ids)
