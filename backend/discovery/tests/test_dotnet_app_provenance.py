"""
R17-A4 / T4 — unit tests for the .NET-app provenance builder (R16-B1 alignment).

Exercises ``discovery.ingest.dotnet_app_provenance`` directly: the artifact-id
references and the observed EvidencePointer it builds. Pins that the builder
REUSES the Evidence & Identity Spine (not a separate model) and always produces a
valid, observed, dotnet_app pointer.
"""
from __future__ import annotations

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest import dotnet_app_provenance as prov

_SPINE = ("source_system", "source_artifact", "source_timestamp", "origin")


# ── artifact id references ─────────────────────────────────────────────────────
def test_log_artifact_id_uses_position_by_default():
    assert prov.log_artifact_id("orders-api", log_offset=7) == "orders-api:log:7"


def test_log_artifact_id_prefers_native_event_id():
    assert prov.log_artifact_id("orders-api", log_offset=7, event_id="evt-1") == "orders-api:log:event:evt-1"


def test_metric_artifact_id_references_app_endpoint_and_sample_time():
    assert prov.metric_artifact_id("orders-api", "2026-06-20T08:00:00+00:00") == "orders-api:metrics:2026-06-20T08:00:00+00:00"


def test_metric_artifact_id_disambiguates_same_timestamp_samples():
    aid = prov.metric_artifact_id("orders-api", "2026-06-20T08:00:00+00:00", seq_index=2)
    assert aid == "orders-api:metrics:2026-06-20T08:00:00+00:00:2"


def test_metric_artifact_id_can_pin_a_specific_metric():
    aid = prov.metric_artifact_id("orders-api", "2026-06-20T08:00:00+00:00", metric_name="cpu-usage")
    assert aid == "orders-api:metrics:cpu-usage:2026-06-20T08:00:00+00:00"


def test_instance_artifact_id():
    assert prov.instance_artifact_id("orders-api", "pod-7") == "orders-api:instance:pod-7"


# ── the pointer ────────────────────────────────────────────────────────────────
def test_source_system_is_fixed_to_dotnet_app():
    assert prov.SOURCE_SYSTEM == "dotnet_app"
    ptr = prov.build_evidence_pointer(source_artifact="x", source_timestamp="2026-06-20T08:00:00+00:00")
    assert ptr["source_system"] == "dotnet_app"


def test_pointer_is_observed_with_no_extraction_job():
    ptr = prov.build_evidence_pointer(source_artifact="x", source_timestamp="2026-06-20T08:00:00+00:00")
    assert ptr["origin"] == OBSERVED
    assert ptr["extraction_job_id"] is None


def test_pointer_reuses_the_spine_and_validates():
    ptr = prov.build_evidence_pointer(
        source_artifact="orders-api:log:3", source_timestamp="2026-06-20T08:01:05+00:00"
    )
    rebuilt = EvidencePointer.from_dict(ptr)
    assert isinstance(rebuilt, EvidencePointer)
    assert rebuilt.is_valid() is True
    assert set(_SPINE).issubset(ptr)
    assert ptr["source_artifact_type"] == "record_id"


def test_missing_timestamp_still_yields_a_valid_pointer():
    ptr = prov.build_evidence_pointer(source_artifact="x", source_timestamp=None)
    assert ptr["source_timestamp"]
    assert EvidencePointer.from_dict(ptr).is_valid() is True


def test_convenience_builders_match_the_artifact_ids():
    log_ptr = prov.build_log_evidence_pointer(
        "orders-api", log_offset=4, event_id="evt-orders-0042",
        source_timestamp="2026-06-20T08:09:15+00:00",
    )
    assert log_ptr["source_artifact"] == "orders-api:log:event:evt-orders-0042"
    assert log_ptr["origin"] == OBSERVED

    metric_ptr = prov.build_metric_evidence_pointer("orders-api", "2026-06-20T08:10:00+00:00")
    assert metric_ptr["source_artifact"] == "orders-api:metrics:2026-06-20T08:10:00+00:00"
    assert metric_ptr["source_timestamp"] == "2026-06-20T08:10:00+00:00"
