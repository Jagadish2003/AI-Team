"""MSP-B0 / AT-638 — tests for raw-payload storage + evidence-pointer resolution.

Covers:
  * T4-AC1 — every normalized event stores an evidence pointer.
  * T4-AC2 — evidence pointers successfully resolve raw payloads.
  * T4-AC3 — evidence remains organization scoped.
  * T4-AC4 — detector-visible models never expose provider payloads directly.
"""
import json
import os

import pytest

from discovery.signals import (
    MAPPERS,
    InMemoryRawEventStore,
    OperationalEvent,
    OrgScopeError,
    ResourceRef,
    map_and_store,
    resolve_raw_event,
    store_raw_event,
)
from discovery.signals.evidence_store import _pointer

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "msp_provider_mapping_golden.json")

# Provider-only envelope keys that must never appear anywhere in a detector-visible
# model (they identify the raw provider payload shape). Curated event fields use
# snake_case keys distinct from these camelCase/hyphenated provider keys.
_PROVIDER_KEYS = {
    "detail-type", "detail", "schemaId", "alertContext", "essentials",
    "eventVersion", "eventID", "eventSource", "eventName", "eventTime",
    "userIdentity", "operationName", "eventTimestamp", "alertTargetIDs",
    "firedDateTime", "resourceId", "eventDataId", "correlationId",
}


def _cases():
    with open(GOLDEN, "r", encoding="utf-8") as fh:
        return json.load(fh)["cases"]


CASES = _cases()


def _all_keys(obj):
    """Recursively collect every dict key in a nested structure."""
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys(item)
    return keys


# ── T4-AC1: every normalized event carries an evidence pointer ──────────────

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_every_event_has_valid_evidence_pointer(case):
    ev = MAPPERS[case["mapper"]](case["raw"], org_id=case["org_id"])
    p = _pointer(ev)
    assert p.is_valid()
    assert p.origin == "observed"
    assert p.source_system and p.source_artifact


def test_directly_constructed_event_also_has_pointer():
    ev = OperationalEvent.build(
        org_id="acme", source_system="aws", signal_id="s1",
        event_type="X", event_class="lifecycle", severity="info",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1"),
    )
    assert _pointer(ev).is_valid()


# ── T4-AC2: pointers resolve the stored raw payload ─────────────────────────

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_pointer_resolves_raw_payload(case):
    store = InMemoryRawEventStore()
    ev = map_and_store(MAPPERS[case["mapper"]], case["raw"], org_id=case["org_id"], store=store)
    resolved = resolve_raw_event(store, case["org_id"], ev)
    assert resolved == case["raw"]


def test_resolve_returns_none_when_nothing_stored():
    store = InMemoryRawEventStore()
    ev = MAPPERS["map_cloudwatch"](CASES[0]["raw"], org_id="acme")
    # never stored -> resolves to None (not an error)
    assert resolve_raw_event(store, "acme", ev) is None


def test_stored_payload_is_copied_not_aliased():
    store = InMemoryRawEventStore()
    raw = {"id": "x", "detail-type": "T", "detail": {"k": 1}}
    ev = map_and_store(MAPPERS["map_eventbridge"], raw, org_id="acme", store=store)
    raw["detail"]["k"] = 999  # mutate caller's copy after storing
    resolved = resolve_raw_event(store, "acme", ev)
    assert resolved["detail"]["k"] == 1  # store kept its own deep copy


# ── T4-AC3: evidence remains organization scoped ────────────────────────────

def test_raw_event_not_visible_to_other_org_in_store():
    store = InMemoryRawEventStore()
    ev = map_and_store(MAPPERS["map_cloudtrail"], CASES[2]["raw"], org_id="org-a", store=store)
    p = _pointer(ev)
    # correct org resolves; a different org sees nothing for the same key
    assert store.get("org-a", p.source_system, p.source_artifact) is not None
    assert store.get("org-b", p.source_system, p.source_artifact) is None


def test_resolve_across_org_boundary_raises():
    store = InMemoryRawEventStore()
    ev = map_and_store(MAPPERS["map_cloudtrail"], CASES[2]["raw"], org_id="org-a", store=store)
    with pytest.raises(OrgScopeError):
        resolve_raw_event(store, "org-b", ev)


def test_store_under_wrong_org_raises():
    store = InMemoryRawEventStore()
    ev = MAPPERS["map_cloudwatch"](CASES[0]["raw"], org_id="org-a")
    with pytest.raises(OrgScopeError):
        store_raw_event(store, "org-b", ev, CASES[0]["raw"])


def test_missing_org_id_raises():
    store = InMemoryRawEventStore()
    with pytest.raises(OrgScopeError):
        store.get("", "aws", "x")


def test_same_signal_id_isolated_between_orgs():
    """Two orgs with a same-id event never read each other's raw payload."""
    store = InMemoryRawEventStore()
    raw_a = {"id": "dup", "detail-type": "T", "detail": {"who": "a"}}
    raw_b = {"id": "dup", "detail-type": "T", "detail": {"who": "b"}}
    ev_a = map_and_store(MAPPERS["map_eventbridge"], raw_a, org_id="org-a", store=store)
    ev_b = map_and_store(MAPPERS["map_eventbridge"], raw_b, org_id="org-b", store=store)
    assert resolve_raw_event(store, "org-a", ev_a)["detail"]["who"] == "a"
    assert resolve_raw_event(store, "org-b", ev_b)["detail"]["who"] == "b"


# ── T4-AC4: detector-visible model never exposes the provider payload ───────

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_detector_visible_model_hides_provider_payload(case):
    ev = MAPPERS[case["mapper"]](case["raw"], org_id=case["org_id"])
    visible = ev.to_dict()
    # no raw provider envelope key appears anywhere in the detector-visible model
    leaked = _all_keys(visible) & _PROVIDER_KEYS
    assert not leaked, f"{case['name']} leaked provider keys: {leaked}"
    # and the raw payload object itself is not embedded
    assert case["raw"] not in visible.values()
    assert visible.get("payload") != case["raw"]
