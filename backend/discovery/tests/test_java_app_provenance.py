"""
R17-A3 / T4 — unit tests for the Java-app provenance builder (R16-B1 alignment).

These exercise ``discovery.ingest.java_app_provenance`` directly: the artifact-id
references and the observed EvidencePointer it builds. They pin that the builder
REUSES the Evidence & Identity Spine (it does not invent a separate model) and
always produces a valid, observed, java_app pointer.
"""
from __future__ import annotations

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest import java_app_provenance as prov

_SPINE = ("source_system", "source_artifact", "source_timestamp", "origin")


# ── artifact id references (doc Section: "Each signal should carry an artifact id") ──
def test_log_artifact_id_uses_position_by_default():
    assert prov.log_artifact_id("payments-api", log_offset=7) == "payments-api:log:7"


def test_log_artifact_id_prefers_native_event_id():
    aid = prov.log_artifact_id("payments-api", log_offset=7, event_id="evt-9f2a")
    assert aid == "payments-api:log:event:evt-9f2a"


def test_actuator_artifact_id_references_app_endpoint_and_sample_time():
    aid = prov.actuator_artifact_id("payments-api", "2026-06-28T10:00:00+00:00")
    assert aid == "payments-api:actuator:2026-06-28T10:00:00+00:00"


def test_actuator_artifact_id_can_pin_a_specific_metric():
    aid = prov.actuator_artifact_id(
        "payments-api", "2026-06-28T10:00:00+00:00",
        metric_name="http.server.requests.error.rate",
    )
    assert aid == "payments-api:actuator:http.server.requests.error.rate:2026-06-28T10:00:00+00:00"


# ── the pointer itself ───────────────────────────────────────────────────────
def test_source_system_is_fixed_to_java_app():
    assert prov.SOURCE_SYSTEM == "java_app"
    ptr = prov.build_evidence_pointer(source_artifact="x", source_timestamp="2026-06-28T10:00:00+00:00")
    assert ptr["source_system"] == "java_app"


def test_pointer_is_observed_with_no_extraction_job():
    ptr = prov.build_evidence_pointer(source_artifact="x", source_timestamp="2026-06-28T10:00:00+00:00")
    assert ptr["origin"] == OBSERVED
    assert ptr["extraction_job_id"] is None


def test_pointer_reuses_the_spine_and_validates():
    # Round-tripping through the spine's own dataclass proves we build the SAME
    # model (no separate provenance shape) and that it is a valid pointer.
    ptr = prov.build_evidence_pointer(
        source_artifact="payments-api:log:3", source_timestamp="2026-06-28T10:00:03+00:00"
    )
    rebuilt = EvidencePointer.from_dict(ptr)
    assert isinstance(rebuilt, EvidencePointer)
    assert rebuilt.is_valid() is True
    assert set(_SPINE).issubset(ptr)


def test_pointer_preserves_artifact_and_timestamp():
    ptr = prov.build_evidence_pointer(
        source_artifact="inventory-api:actuator:2026-06-28T09:30:00+00:00",
        source_timestamp="2026-06-28T09:30:00+00:00",
    )
    assert ptr["source_artifact"] == "inventory-api:actuator:2026-06-28T09:30:00+00:00"
    assert ptr["source_timestamp"] == "2026-06-28T09:30:00+00:00"
    assert ptr["source_artifact_type"] == "record_id"


def test_missing_timestamp_still_yields_a_valid_pointer():
    # The mandatory spine must always be populated — a missing observation time
    # defaults to now rather than leaving the pointer invalid.
    ptr = prov.build_evidence_pointer(source_artifact="x", source_timestamp=None)
    assert ptr["source_timestamp"]
    assert EvidencePointer.from_dict(ptr).is_valid() is True


def test_convenience_builders_match_the_artifact_ids():
    log_ptr = prov.build_log_evidence_pointer(
        "payments-api", log_offset=4, event_id="evt-paym-0042",
        source_timestamp="2026-06-28T10:00:42+00:00",
    )
    assert log_ptr["source_artifact"] == "payments-api:log:event:evt-paym-0042"
    assert log_ptr["origin"] == OBSERVED

    act_ptr = prov.build_actuator_evidence_pointer("payments-api", "2026-06-28T10:00:00+00:00")
    assert act_ptr["source_artifact"] == "payments-api:actuator:2026-06-28T10:00:00+00:00"
    assert act_ptr["source_timestamp"] == "2026-06-28T10:00:00+00:00"
