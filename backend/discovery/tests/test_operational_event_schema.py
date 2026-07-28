"""MSP-B0 / AT-635 — tests for the Operational Event Schema.

Covers the two acceptance criteria:
  * T1-AC1 — standardized vocabularies for resource_type, event_class, severity.
  * T1-AC3 — the schema is the common contract for AWS, Azure, and the Event
    History Bridge (same normalised shape produced from each provider's native
    payload).
"""
import pytest

from discovery.signals.operational_event import (
    EVENT_CLASSES,
    RESOURCE_TYPES,
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    CommonSignal,
    OperationalEvent,
    ResourceRef,
    normalize_event_class,
    normalize_resource_type,
    normalize_severity,
)


def _valid_provenance(source_system="aws", signal_id="evt-1"):
    from app.provenance import EvidencePointer
    return EvidencePointer.observed(
        source_system=source_system,
        source_artifact=signal_id,
        source_artifact_type="record_id",
    ).to_dict()


# ── T1-AC1: vocabularies are defined and closed ─────────────────────────────

def test_vocabularies_defined_and_nonempty():
    assert RESOURCE_TYPES and EVENT_CLASSES and SEVERITY_LEVELS
    # every severity has a rank
    assert set(SEVERITY_ORDER) == set(SEVERITY_LEVELS)


def test_severity_ordering_is_monotonic():
    assert SEVERITY_ORDER["critical"] > SEVERITY_ORDER["high"] > SEVERITY_ORDER["medium"]
    assert SEVERITY_ORDER["medium"] > SEVERITY_ORDER["low"] > SEVERITY_ORDER["info"]


@pytest.mark.parametrize("field_name,bad_value", [
    ("resource_type", "not_a_type"),
    ("event_class", "not_a_class"),
    ("severity", "sev-unknown"),
])
def test_out_of_vocabulary_values_rejected(field_name, bad_value):
    kwargs = dict(
        org_id="acme",
        source_system="aws",
        signal_id="evt-1",
        observed_at="2026-07-14T00:00:00+00:00",
        provenance=_valid_provenance(),
        event_type="SomeEvent",
        resource_type="compute",
        event_class="lifecycle",
        severity="high",
    )
    kwargs[field_name] = bad_value
    with pytest.raises(ValueError, match="must be one of"):
        OperationalEvent(**kwargs)


# ── normalisation helpers map provider-native tokens onto the vocabulary ────

@pytest.mark.parametrize("raw,expected", [
    ("CRITICAL", "critical"), ("Sev0", "critical"), ("P1", "critical"),
    ("Error", "high"), ("Warning", "medium"), ("Informational", "info"),
    ("totally-unknown", "info"), (None, "info"), ("", "info"),
])
def test_normalize_severity(raw, expected):
    assert normalize_severity(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("EC2", "compute"), ("VirtualMachine", "compute"), ("Lambda", "serverless"),
    ("S3", "storage"), ("Blob", "storage"), ("RDS", "database"),
    ("VNet", "network"), ("IAM", "identity"), ("unknown-thing", "other"),
])
def test_normalize_resource_type(raw, expected):
    assert normalize_resource_type(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Create", "lifecycle"), ("Delete", "lifecycle"), ("Update", "configuration"),
    ("Login", "access"), ("Failure", "error"), ("Latency", "performance"),
    ("Finding", "security"), ("Compliance", "audit"), ("weird", "other"),
])
def test_normalize_event_class(raw, expected):
    assert normalize_event_class(raw) == expected


# ── org scoping (CommonSignal spine) ────────────────────────────────────────

def test_org_id_required():
    with pytest.raises(ValueError, match="org_id"):
        OperationalEvent(
            org_id="",
            source_system="aws",
            signal_id="evt-1",
            observed_at="2026-07-14T00:00:00+00:00",
            provenance=_valid_provenance(),
            event_type="X",
        )


def test_invalid_provenance_rejected():
    with pytest.raises(ValueError, match="provenance"):
        OperationalEvent(
            org_id="acme",
            source_system="aws",
            signal_id="evt-1",
            observed_at="2026-07-14T00:00:00+00:00",
            provenance={},  # empty spine is invalid
            event_type="X",
        )


def test_common_signal_is_the_spine():
    assert issubclass(OperationalEvent, CommonSignal)


# ── ResourceRef ─────────────────────────────────────────────────────────────

def test_resource_ref_validates_type_and_required_fields():
    ref = ResourceRef(provider="aws", resource_type="compute", resource_id="i-123")
    assert ref.to_dict()["resource_id"] == "i-123"
    with pytest.raises(ValueError):
        ResourceRef(provider="aws", resource_type="bogus", resource_id="i-1")
    with pytest.raises(ValueError):
        ResourceRef(provider="", resource_type="compute", resource_id="i-1")


# ── T1-AC3: same normalised shape from each provider's native payload ───────

def test_build_normalises_aws_payload():
    ref = ResourceRef(provider="aws", resource_type="compute", resource_id="arn:aws:ec2:...:i-1", region="us-east-1")
    ev = OperationalEvent.build(
        org_id="acme",
        source_system="aws",
        signal_id="aws-evt-1",
        event_type="EC2 Instance State-change Notification",
        resource=ref,
        event_class="stop",
        severity="Warning",
    )
    assert ev.resource_type == "compute"
    assert ev.event_class == "lifecycle"
    assert ev.severity == "medium"
    assert ev.severity_rank == SEVERITY_ORDER["medium"]
    assert ev.provenance["origin"] == "observed"


def test_build_normalises_azure_payload():
    ref = ResourceRef(
        provider="azure",
        resource_type=normalize_resource_type("virtualMachine"),
        resource_id="/subscriptions/.../vm1",
    )
    ev = OperationalEvent.build(
        org_id="acme",
        source_system="azure",
        signal_id="azure-evt-1",
        event_type="Microsoft.Compute/virtualMachines/write",
        resource=ref,
        event_class="Update",
        severity="Sev1",
    )
    assert ev.resource_type == "compute"
    assert ev.event_class == "configuration"
    assert ev.severity == "critical"


def test_build_from_bridge_and_all_providers_share_one_shape():
    """AWS, Azure, and the export bridge all yield the identical dict key set."""
    common = dict(org_id="acme", event_type="X", event_class="error", severity="high")
    aws = OperationalEvent.build(source_system="aws", signal_id="a", **common)
    azure = OperationalEvent.build(source_system="azure", signal_id="b", **common)
    bridge = OperationalEvent.build(source_system="event_bridge", signal_id="c", **common)
    assert aws.to_dict().keys() == azure.to_dict().keys() == bridge.to_dict().keys()
    # resource omitted → defaults, still valid & normalised
    assert bridge.resource_type == "other"
    assert bridge.severity == "high"


def test_to_dict_is_json_serialisable():
    import json
    ref = ResourceRef(provider="aws", resource_type="storage", resource_id="my-bucket")
    ev = OperationalEvent.build(
        org_id="acme", source_system="aws", signal_id="evt-9",
        event_type="PutObject", resource=ref, event_class="write", severity="info",
    )
    # round-trips through json without error
    dumped = json.dumps(ev.to_dict())
    assert '"resource_type": "storage"' in dumped
