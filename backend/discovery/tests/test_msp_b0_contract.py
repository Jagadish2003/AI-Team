"""MSP-B0 / AT-640 — the Operational Event Schema contract suite (Section 3).

This is the consolidated contract for the "one model for every cloud" guarantee:
whatever the provider, a normalised event is one shape, provider payloads are
unreachable through it, its signature is deterministic, its raw payload is
traceable, and tenant boundaries hold. The headline is the **detector-blindness
test** — a detector written against the schema behaves identically regardless of
the source provider and physically cannot reach a provider-specific field.

Each test is labelled with the acceptance criterion it discharges:
  * T6-AC1 — AWS and Azure fixtures normalize to equivalent detector-visible structures.
  * T6-AC2 — provider-specific fields are inaccessible through the normalized model.
  * T6-AC3 — event signatures remain deterministic across repeated events.
  * T6-AC4 — evidence pointers resolve correctly for every normalized event.
  * T6-AC5 — cross-tenant isolation is validated.
  * T6-AC6 — mapping contract documentation is sufficient for B1/B2/B8.

These are pure-Python contract tests (in-memory evidence store, no DB) so they
live alongside the other MSP-B0 signal tests and run without the contract DB.
"""
import json
import os

import pytest

from discovery.signals import (
    EVENT_CLASSES,
    RESOURCE_TYPES,
    SEVERITY_LEVELS,
    InMemoryRawEventStore,
    MAPPERS,
    OperationalEvent,
    OrgScopeError,
    ResourceRef,
    compute_event_signature,
    map_and_store,
    provider_family,
    resolve_raw_event,
)

_HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(_HERE, "fixtures", "msp_provider_mapping_golden.json")
DOC = os.path.join(_HERE, "..", "..", "..", "docs", "msp_operational_event_schema.md")

# Provider-only envelope keys — none may be reachable through the normalized model.
_PROVIDER_KEYS = {
    "detail-type", "detail", "schemaId", "alertContext", "essentials",
    "eventVersion", "eventID", "eventSource", "eventName", "eventTime",
    "userIdentity", "operationName", "eventTimestamp", "alertTargetIDs",
    "firedDateTime", "resourceId", "eventDataId", "correlationId", "awsRegion",
}


def _cases():
    with open(GOLDEN, "r", encoding="utf-8") as fh:
        return json.load(fh)["cases"]


CASES = _cases()
AWS_CASES = [c for c in CASES if c["expected"]["provider_family"] == "aws"]
AZURE_CASES = [c for c in CASES if c["expected"]["provider_family"] == "azure"]


def _map(case):
    return MAPPERS[case["mapper"]](case["raw"], org_id=case["org_id"])


def _all_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys(item)
    return keys


def _structure(event):
    """Top-level schema key set + resource sub-structure — the detector-visible
    structure. The free-form ``payload`` bag is intentionally excluded: detectors
    reason over the fixed schema fields, not over payload."""
    d = event.to_dict()
    top = frozenset(d.keys())
    res = frozenset(d["resource"].keys()) if d.get("resource") else frozenset()
    return top, res


# ── T6-AC1: AWS & Azure normalize to equivalent detector-visible structures ──

def test_all_providers_share_one_detector_visible_structure():
    structures = {_structure(_map(c)) for c in CASES}
    assert len(structures) == 1, f"providers diverged in structure: {structures}"


def test_aws_and_azure_structures_match():
    assert _structure(_map(AWS_CASES[0])) == _structure(_map(AZURE_CASES[0]))


def test_equivalent_aws_azure_events_normalize_identically():
    """Semantically equivalent events from different providers yield the same
    normalised vocabulary values (a detector sees the same thing)."""
    aws = OperationalEvent.build(
        org_id="acme", source_system="aws", signal_id="a", event_type="Some Error",
        event_class="error", severity="Sev2",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1"),
    )
    azure = OperationalEvent.build(
        org_id="acme", source_system="azure", signal_id="b", event_type="Some.Error",
        event_class="error", severity="Sev2",
        resource=ResourceRef(provider="azure", resource_type="compute", resource_id="/s/vm1"),
    )
    assert (aws.event_class, aws.resource_type, aws.severity) == \
           (azure.event_class, azure.resource_type, azure.severity)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_normalised_values_are_in_vocabulary(case):
    ev = _map(case)
    assert ev.resource_type in RESOURCE_TYPES
    assert ev.event_class in EVENT_CLASSES
    assert ev.severity in SEVERITY_LEVELS


# ── T6-AC2: provider-specific fields inaccessible through the model ─────────

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_no_provider_field_reachable_through_model(case):
    ev = _map(case)
    leaked = _all_keys(ev.to_dict()) & _PROVIDER_KEYS
    assert not leaked, f"{case['name']} leaked provider keys: {leaked}"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_raw_payload_not_embedded_in_model(case):
    ev = _map(case)
    assert case["raw"] not in ev.to_dict().values()


# ── The detector-blindness test (headline) ──────────────────────────────────

def _friction_detector(events):
    """A representative detector: it reasons ONLY over normalised schema fields
    (severity + event_class + resource_type). It has no knowledge of providers
    and no access to provider payloads."""
    counts = {}
    for ev in events:
        d = ev.to_dict()
        if d["severity"] in ("critical", "high") and d["event_class"] in ("error", "state_change"):
            counts[d["resource_type"]] = counts.get(d["resource_type"], 0) + 1
    return counts


def test_detector_is_blind_to_provider():
    """The same detector produces identical results for equivalent AWS and Azure
    events — it never branches on, or needs, the provider."""
    aws_events = [
        OperationalEvent.build(org_id="acme", source_system="aws", signal_id="a1",
                               event_type="X", event_class="error", severity="high",
                               resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1")),
        OperationalEvent.build(org_id="acme", source_system="aws", signal_id="a2",
                               event_type="Y", event_class="state_change", severity="critical",
                               resource=ResourceRef(provider="aws", resource_type="storage", resource_id="b-1")),
    ]
    azure_events = [
        OperationalEvent.build(org_id="acme", source_system="azure", signal_id="z1",
                               event_type="X", event_class="error", severity="high",
                               resource=ResourceRef(provider="azure", resource_type="compute", resource_id="/s/vm1")),
        OperationalEvent.build(org_id="acme", source_system="azure", signal_id="z2",
                               event_type="Y", event_class="state_change", severity="critical",
                               resource=ResourceRef(provider="azure", resource_type="storage", resource_id="/s/sa1")),
    ]
    assert _friction_detector(aws_events) == _friction_detector(azure_events) == {"compute": 1, "storage": 1}


def test_detector_cannot_access_provider_payload():
    """A detector physically cannot reach a provider-specific field — it is not
    on the model (KeyError), so no detector can come to depend on one."""
    ev = _map(AWS_CASES[0])
    d = ev.to_dict()
    for provider_field in ("detail-type", "detail", "eventSource", "schemaId"):
        assert provider_field not in d
        with pytest.raises(KeyError):
            _ = d[provider_field]


# ── T6-AC3: event signatures deterministic across repeated events ───────────

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_signature_deterministic_on_remap(case):
    a = _map(case).event_signature
    b = _map(case).event_signature
    assert a == b and a.startswith("1:")


def test_signature_stable_across_repeated_occurrences():
    """Repeated occurrences (different time / id / severity) share one signature."""
    base = dict(
        eventSource="sts.amazonaws.com", eventName="AssumeRole", awsRegion="us-east-1",
        userIdentity={"arn": "arn:aws:iam::1:user/alice"},
        resources=[{"ARN": "arn:aws:iam::1:role/admin", "type": "AWS::IAM::Role"}],
    )
    sigs = {
        MAPPERS["map_cloudtrail"]({**base, "eventID": str(i), "eventTime": f"2026-07-1{i}T00:00:00Z"},
                                  org_id="acme").event_signature
        for i in range(1, 4)
    }
    assert len(sigs) == 1


def test_signature_changes_when_identity_changes():
    s1 = compute_event_signature(source_system="aws", event_class="error",
                                 resource_type="compute", event_type="X", resource_id="i-1")
    s2 = compute_event_signature(source_system="aws", event_class="error",
                                 resource_type="compute", event_type="X", resource_id="i-2")
    assert s1 != s2


# ── T6-AC4: evidence pointers resolve for every normalized event ────────────

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_evidence_resolves_for_every_event(case):
    store = InMemoryRawEventStore()
    ev = map_and_store(MAPPERS[case["mapper"]], case["raw"], org_id=case["org_id"], store=store)
    assert resolve_raw_event(store, case["org_id"], ev) == case["raw"]


# ── T6-AC5: cross-tenant isolation ──────────────────────────────────────────

def test_cross_tenant_resolution_blocked():
    store = InMemoryRawEventStore()
    ev = map_and_store(MAPPERS["map_cloudtrail"], AWS_CASES[-1]["raw"], org_id="tenant-a", store=store)
    with pytest.raises(OrgScopeError):
        resolve_raw_event(store, "tenant-b", ev)


def test_cross_tenant_store_lookup_returns_nothing():
    store = InMemoryRawEventStore()
    ev = map_and_store(MAPPERS["map_cloudwatch"], AWS_CASES[0]["raw"], org_id="tenant-a", store=store)
    from discovery.signals.evidence_store import _pointer
    p = _pointer(ev)
    assert store.get("tenant-a", p.source_system, p.source_artifact) is not None
    assert store.get("tenant-b", p.source_system, p.source_artifact) is None


def test_same_event_id_isolated_between_tenants():
    store = InMemoryRawEventStore()
    raw_a = {"id": "shared", "detail-type": "T", "detail": {"t": "a"}}
    raw_b = {"id": "shared", "detail-type": "T", "detail": {"t": "b"}}
    ev_a = map_and_store(MAPPERS["map_eventbridge"], raw_a, org_id="tenant-a", store=store)
    ev_b = map_and_store(MAPPERS["map_eventbridge"], raw_b, org_id="tenant-b", store=store)
    assert resolve_raw_event(store, "tenant-a", ev_a)["detail"]["t"] == "a"
    assert resolve_raw_event(store, "tenant-b", ev_b)["detail"]["t"] == "b"


def test_provider_family_resolution_is_stable():
    # a normalised event's provider family is derivable + consistent per source
    for case in CASES:
        ev = _map(case)
        assert provider_family(ev.source_system) == case["expected"]["provider_family"]


# ── T6-AC6: mapping contract documentation sufficient for B1/B2/B8 ──────────

def test_mapping_contract_doc_is_sufficient_for_connector_implementers():
    assert os.path.exists(DOC), "mapping contract doc is missing"
    with open(DOC, "r", encoding="utf-8") as fh:
        text = fh.read()
    # Every reference mapper an implementer needs is documented by name.
    for mapper_name in MAPPERS:
        assert mapper_name in text, f"doc missing mapper {mapper_name}"
    # The pieces B1/B2/B8 need to implement a provider without extra clarification.
    required = [
        "Provider mapping contract",     # the section
        "Field-by-field mapping",        # the per-field map
        "resource_type", "event_class", "severity",   # the vocabularies
        "event_signature",               # signature rules
        "Organization isolation",        # tenant scoping rules
        "Adding a new provider",         # the extension recipe
        "OperationalEvent.build",        # the terminal call every mapper makes
    ]
    for token in required:
        assert token in text, f"doc missing required guidance: {token!r}"
