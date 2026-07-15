"""MSP-B0 / AT-636 — tests for deterministic event_signature construction.

Covers:
  * T2-AC1 — signatures are deterministic across repeated occurrences.
  * T2-AC2 — different operational events generate unique signatures.
  * T2-AC3 — the rules are documented (asserted structurally via
    signature_components() exposing the resolved recipe).
  * T2-AC4 — construction rules validated using provider-specific fixtures.
"""
import json
import os

import pytest

from discovery.signals import (
    EVENT_SIGNATURE_VERSION,
    OperationalEvent,
    ResourceRef,
    compute_event_signature,
    provider_family,
    signature_components,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "msp_event_signatures.json")


def _load():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _event(spec):
    res = spec.get("resource")
    ref = ResourceRef(**res) if res else None
    return OperationalEvent.build(
        org_id=spec["org_id"],
        source_system=spec["source_system"],
        signal_id=spec["signal_id"],
        observed_at=spec.get("observed_at"),
        event_type=spec["event_type"],
        event_class=spec.get("event_class"),
        severity=spec.get("severity"),
        resource=ref,
        message=spec.get("message"),
        payload=spec.get("payload"),
    )


# ── T2-AC1: deterministic across repeated occurrences ───────────────────────

def test_signature_is_pure_function_of_identity():
    sig1 = compute_event_signature(
        source_system="aws", event_class="error", resource_type="compute",
        event_type="RunInstances", resource_id="i-1",
    )
    sig2 = compute_event_signature(
        source_system="aws", event_class="error", resource_type="compute",
        event_type="RunInstances", resource_id="i-1",
    )
    assert sig1 == sig2
    assert sig1.startswith(f"{EVENT_SIGNATURE_VERSION}:")


def test_timestamp_signalid_severity_message_excluded():
    base = dict(org_id="acme", source_system="aws", event_type="RunInstances",
                event_class="lifecycle", resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1"))
    a = OperationalEvent.build(signal_id="s1", observed_at="2026-01-01T00:00:00+00:00", severity="critical", message="a", **base)
    b = OperationalEvent.build(signal_id="s2", observed_at="2026-12-31T23:59:59+00:00", severity="info", message="b", **base)
    assert a.event_signature == b.event_signature


@pytest.mark.parametrize("group", _load()["recurring_groups"], ids=lambda g: g["name"])
def test_fixture_recurring_occurrences_share_signature(group):
    """T2-AC1 + T2-AC4: each provider fixture group's occurrences collapse to one signature."""
    sigs = {_event(occ).event_signature for occ in group["occurrences"]}
    assert len(sigs) == 1, f"{group['name']} occurrences did not collapse: {sigs}"
    # and the family resolved as the fixture declares
    fam = provider_family(group["occurrences"][0]["source_system"])
    assert fam == group["provider_family"]


# ── T2-AC2: different events are unique ──────────────────────────────────────

@pytest.mark.parametrize("pair", _load()["distinct_pairs"], ids=lambda p: p["why"])
def test_fixture_distinct_pairs_differ(pair):
    """T2-AC2 + T2-AC4: genuinely different events never collide."""
    assert _event(pair["a"]).event_signature != _event(pair["b"]).event_signature


def test_all_fixture_signatures_globally_unique_across_groups():
    """No two distinct recurring groups accidentally share a signature."""
    data = _load()
    per_group = {g["name"]: _event(g["occurrences"][0]).event_signature for g in data["recurring_groups"]}
    assert len(set(per_group.values())) == len(per_group), per_group


# ── T2-AC3: rules are documented / introspectable ───────────────────────────

def test_signature_components_expose_the_recipe():
    comps = signature_components(
        source_system="aws_cloudtrail", event_class="access", resource_type="identity",
        event_type="AssumeRole", resource_id="role/admin", principal="user/alice",
    )
    assert comps["provider_family"] == "aws"
    # access recipe includes principal (documented per-class rule)
    assert "principal" in comps["recipe"]
    assert comps["event_type_normalized"] == "assumerole"


def test_access_recipe_includes_principal_but_lifecycle_does_not():
    access = signature_components(source_system="aws", event_class="access", resource_type="identity", event_type="x")
    lifecycle = signature_components(source_system="aws", event_class="lifecycle", resource_type="compute", event_type="x")
    assert "principal" in access["recipe"]
    assert "principal" not in lifecycle["recipe"]


def test_provider_family_normalisation_of_event_type():
    # AWS: spaces collapse to underscores
    aws = signature_components(source_system="aws", event_class="state_change", resource_type="compute",
                               event_type="EC2 Instance State-change Notification")
    assert aws["event_type_normalized"] == "ec2_instance_state-change_notification"
    # Azure: slash path preserved, lowercased
    az = signature_components(source_system="azure", event_class="configuration", resource_type="compute",
                              event_type="Microsoft.Compute/virtualMachines/write")
    assert az["event_type_normalized"] == "microsoft.compute/virtualmachines/write"


def test_event_auto_populates_signature():
    ev = OperationalEvent.build(
        org_id="acme", source_system="aws", signal_id="s1",
        event_type="RunInstances", event_class="lifecycle", severity="info",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1"),
    )
    assert ev.event_signature
    assert ev.event_signature == compute_event_signature(
        source_system="aws", event_class="lifecycle", resource_type="compute",
        event_type="RunInstances", resource_id="i-1",
    )


def test_explicit_signature_is_respected():
    ev = OperationalEvent(
        org_id="acme", source_system="aws", signal_id="s1",
        observed_at="2026-07-14T00:00:00+00:00",
        provenance={"source_system": "aws", "source_artifact": "s1", "source_timestamp": "2026-07-14T00:00:00+00:00", "origin": "observed"},
        event_type="X", resource_type="compute", event_class="lifecycle", severity="info",
        event_signature="1:deadbeef",
    )
    assert ev.event_signature == "1:deadbeef"
