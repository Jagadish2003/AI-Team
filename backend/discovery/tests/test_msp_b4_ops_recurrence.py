"""MSP-B4 T3 / AC1-AC2 recurrence-detector contract tests."""
from __future__ import annotations

import json

import pytest

from app.provenance import EvidencePointer
from discovery.signals.resolution_signature import (
    compute_incident_identity_signature,
    compute_resolution_signature,
)


def _incident(
    number: int,
    *,
    org_id: str = "org-a",
    category: str = "software",
    close_code: str = "Solved (Permanently)",
    ci_class: str = "cmdb_ci_server",
    group: str = "Platform Operations",
    short_description: str = "Portal email service unavailable",
    resolved_at: str = "2026-07-10 12:00:00",
    ttr: int | None = 3600,
    note: str = "",
) -> dict:
    incident_sys_id = f"incident-sys-{number:04d}"
    identity_signature = compute_incident_identity_signature(
        category=category,
        short_description=short_description,
        ci_class=ci_class,
    )
    resolution_signature = compute_resolution_signature(
        category=category,
        close_code=close_code,
        resolved_by_group=group,
        ci_class=ci_class,
    )
    evidence = EvidencePointer.observed(
        source_system="servicenow",
        source_artifact=incident_sys_id,
        source_timestamp=resolved_at,
        source_artifact_type="record_id",
    ).to_dict()
    return {
        "sys_id": incident_sys_id,
        "number": f"INC{number:07d}",
        "org_id": org_id,
        "category": category,
        "ci_class": ci_class,
        "short_description": short_description,
        "assignment_group": group,
        "close_code": close_code,
        "resolved_at": resolved_at,
        "close_notes": note,
        # Person-level noise that must not be copied into detector output.
        "assigned_to": "Alice Example",
        "caller_id": "Bob Example",
        "resolved_by": "Carol Example",
        "source_url": (
            "https://acme.service-now.com/nav_to.do?"
            f"uri=incident.do%3Fsys_id%3D{incident_sys_id}"
        ),
        "resolution": {
            "is_resolved": True,
            "resolution_category": category,
            "close_code": close_code,
            "resolved_by_group": group,
            "resolved_at": resolved_at,
            "time_to_resolve_seconds": ttr,
            "incident_identity_signature": identity_signature,
            "resolution_signature": resolution_signature,
            "evidence": evidence,
        },
    }


def _payload(*incidents: dict, org_id: str = "org-a", **extra) -> dict:
    return {
        "org_id": org_id,
        "incident_metrics": {
            "org_id": org_id,
            "incidents": list(incidents),
        },
        **extra,
    }


def _three_matching() -> list[dict]:
    return [
        _incident(1, resolved_at="2026-07-10 12:00:00", ttr=3600),
        _incident(2, resolved_at="2026-07-12 12:00:00", ttr=7200),
        _incident(3, resolved_at="2026-07-14 12:00:00", ttr=10800),
    ]


def test_matching_incidents_emit_complete_recurrence_record():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    records = find_recurrences(
        _payload(*_three_matching()),
        config=RecurrenceConfig(floor=3, window_days=30, max_examples=3),
        as_of="2026-07-15 12:00:00",
    )

    assert len(records) == 1
    record = records[0]
    assert record.recurrence_count == 3
    assert record.count == 3
    assert record.recurrence_floor == 3
    assert record.evaluated_window == {
        "start": "2026-06-15T12:00:00+00:00",
        "end": "2026-07-15T12:00:00+00:00",
        "days": 30,
    }
    assert record.median_time_to_resolve_seconds == 7200
    assert record.median_ttr == 7200
    assert record.window == record.evaluated_window
    assert record.grouped_signatures == {
        "incident_identity_signature": record.incident_identity_signature,
        "resolution_signature": record.resolution_signature,
    }
    assert record.measured_ttr_count == 3
    assert record.total_time_to_resolve_seconds == 21600
    assert record.incident_identity_signature.startswith("1:")
    assert record.resolution_signature.startswith("1:")
    assert len(record.examples) == 3
    assert len(record.example_evidence_pointers) == 3
    assert all(
        EvidencePointer.from_dict(pointer).is_valid()
        for pointer in record.example_evidence_pointers
    )
    assert [example["incident_sys_id"] for example in record.examples] == [
        "incident-sys-0001", "incident-sys-0002", "incident-sys-0003"
    ]


def test_configurable_floor_and_window_change_sensitivity_without_code_changes():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    data = _payload(*_three_matching())
    assert len(
        find_recurrences(
            data,
            config=RecurrenceConfig(floor=3, window_days=30),
            as_of="2026-07-15 12:00:00",
        )
    ) == 1
    assert find_recurrences(
        data,
        config=RecurrenceConfig(floor=4, window_days=30),
        as_of="2026-07-15 12:00:00",
    ) == []
    assert find_recurrences(
        data,
        config=RecurrenceConfig(floor=3, window_days=3),
        as_of="2026-07-15 12:00:00",
    ) == []
    assert len(
        find_recurrences(
            data,
            config=RecurrenceConfig(floor=2, window_days=3),
            as_of="2026-07-15 12:00:00",
        )
    ) == 1


def test_payload_configuration_is_used_by_detector_entrypoint():
    from discovery.detectors.ops_recurrence import detect

    data = _payload(
        *_three_matching(),
        recurrence_config={"floor": 3, "window_days": 30, "max_examples": 2},
        as_of="2026-07-15 12:00:00",
    )
    results = detect({}, data)

    assert len(results) == 1
    assert results[0].threshold == 3
    assert len(results[0].raw_evidence["examples"]) == 2


def test_same_category_different_close_code_is_a_near_miss_not_grouped():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    near_miss = _incident(
        9,
        close_code="Solved (Work Around)",
        resolved_at="2026-07-14 13:00:00",
    )
    records = find_recurrences(
        _payload(*_three_matching(), near_miss),
        config=RecurrenceConfig(floor=3, window_days=30),
        as_of="2026-07-15 12:00:00",
    )

    assert len(records) == 1
    assert records[0].recurrence_count == 3
    assert "incident-sys-0009" not in {
        example["incident_sys_id"] for example in records[0].examples
    }


def test_same_close_code_different_ci_class_is_a_near_miss_not_grouped():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    near_miss = _incident(
        8,
        ci_class="cmdb_ci_db_instance",
        resolved_at="2026-07-14 13:00:00",
    )
    records = find_recurrences(
        _payload(*_three_matching(), near_miss),
        config=RecurrenceConfig(floor=3, window_days=30),
        as_of="2026-07-15 12:00:00",
    )

    assert len(records) == 1
    assert records[0].recurrence_count == 3
    assert records[0].signature_components["resolution"]["ci_component"] == (
        "class:cmdb_ci_server"
    )


def test_resolution_notes_and_individuals_never_enter_grouping_or_output():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    incidents = _three_matching()
    incidents[0]["close_notes"] = "Alice Example used password=hunter2"
    incidents[1]["close_notes"] = "Completely different prose by Dana Example"
    incidents[2]["close_notes"] = "Embedding similarity must not be used"

    record = find_recurrences(
        _payload(*incidents),
        config=RecurrenceConfig(floor=3, window_days=30),
        as_of="2026-07-15 12:00:00",
    )[0]
    rendered = json.dumps(record.as_dict())

    for forbidden in (
        "Alice Example", "Bob Example", "Carol Example", "Dana Example",
        "hunter2", "Completely different prose", "Embedding similarity",
        "assigned_to", "caller_id", '"resolved_by":', "close_notes",
    ):
        assert forbidden not in rendered
    assert "short_description_tokens" not in rendered
    assert "resolved_by_assignment_group" in rendered


def test_missing_ttr_does_not_change_count_and_median_uses_measured_values():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    incidents = _three_matching()
    incidents[1]["resolution"]["time_to_resolve_seconds"] = None
    record = find_recurrences(
        _payload(*incidents),
        config=RecurrenceConfig(floor=3, window_days=30),
        as_of="2026-07-15 12:00:00",
    )[0]

    assert record.recurrence_count == 3
    assert record.measured_ttr_count == 2
    assert record.median_time_to_resolve_seconds == 7200


def test_same_health_state_is_deterministic_even_when_input_order_changes():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    incidents = _three_matching()
    kwargs = {
        "config": RecurrenceConfig(floor=3, window_days=30),
        "as_of": "2026-07-15 12:00:00",
    }
    forward = [record.as_dict() for record in find_recurrences(_payload(*incidents), **kwargs)]
    reverse = [
        record.as_dict()
        for record in find_recurrences(_payload(*reversed(incidents)), **kwargs)
    ]

    assert forward == reverse


def test_two_org_scope_never_counts_another_orgs_incident():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    org_a = _three_matching()
    org_b = _incident(
        99,
        org_id="org-b",
        resolved_at="2026-07-14 13:00:00",
    )
    records = find_recurrences(
        _payload(*org_a, org_b, org_id="org-a"),
        config=RecurrenceConfig(floor=3, window_days=30),
        as_of="2026-07-15 12:00:00",
        org_id="org-a",
    )

    assert len(records) == 1
    assert records[0].org_id == "org-a"
    assert records[0].recurrence_count == 3
    assert all(
        example["incident_sys_id"] != "incident-sys-0099"
        for example in records[0].examples
    )


def test_mixed_org_input_without_scope_fails_closed():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    incidents = _three_matching()
    incidents.append(
        _incident(99, org_id="org-b", resolved_at="2026-07-14 13:00:00")
    )
    data = {"incident_metrics": {"incidents": incidents}}

    with pytest.raises(ValueError, match="org_id is required"):
        find_recurrences(
            data,
            config=RecurrenceConfig(floor=3, window_days=30),
            as_of="2026-07-15 12:00:00",
        )


def test_structured_fields_are_used_when_precomputed_signatures_are_absent():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, find_recurrences

    incidents = _three_matching()
    for incident in incidents:
        incident["resolution"].pop("incident_identity_signature")
        incident["resolution"].pop("resolution_signature")

    records = find_recurrences(
        _payload(*incidents),
        config=RecurrenceConfig(floor=3, window_days=30),
        as_of="2026-07-15 12:00:00",
    )
    assert len(records) == 1
    assert records[0].recurrence_count == 3


@pytest.mark.parametrize(
    "config",
    [
        {"floor": 1},
        {"window_days": 0},
        {"max_examples": 0},
        {"floor": "not-a-number"},
    ],
)
def test_invalid_configuration_fails_loudly(config):
    from discovery.detectors.ops_recurrence import resolve_recurrence_config

    with pytest.raises(ValueError):
        resolve_recurrence_config({}, config)


def test_evaluate_and_detect_agree_on_firing_and_metrics():
    from discovery.detectors.ops_recurrence import RecurrenceConfig, detect, evaluate

    data = _payload(*_three_matching())
    config = RecurrenceConfig(floor=3, window_days=30)
    evaluation = evaluate(
        {}, data, config=config, as_of="2026-07-15 12:00:00"
    )
    results = detect({}, data, config=config, as_of="2026-07-15 12:00:00")

    assert evaluation.fired is True
    assert evaluation.metric_value == 3
    assert evaluation.raw_evidence["recurrence_loop_count"] == 1
    assert evaluation.raw_evidence["max_median_time_to_resolve_seconds"] == 7200
    assert len(results) == 1
    assert results[0].metric_value == 3
