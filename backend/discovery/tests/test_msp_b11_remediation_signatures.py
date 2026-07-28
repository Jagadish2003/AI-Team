"""MSP-B11 T4 / AC4 - deterministic remediation-pattern signatures."""
from __future__ import annotations

import json
from copy import deepcopy

from app.vulnerable_item_ci_resolution import resolve_vulnerable_item_ci_references
from database.models.entities import Entity
from discovery.signals.remediation_signature import (
    REMEDIATION_SIGNATURE_EXCLUDED_FIELDS,
    REMEDIATION_SIGNATURE_INCLUDED_FIELDS,
    REMEDIATION_SIGNATURE_VERSION,
    apply_remediation_signature,
    apply_remediation_signatures,
    compute_remediation_signature,
    normalize_remediation_path,
    remediation_path_from_history,
    remediation_signature_components,
)


_HISTORY = [
    {
        "field": "state",
        "from_value": "Detected",
        "to_value": "Assigned",
        "changed_at": "2026-07-01 09:00:00",
    },
    {
        "field": "state",
        "from_value": "Assigned",
        "to_value": "In Progress",
        "changed_at": "2026-07-02 09:00:00",
    },
    {
        "field": "state",
        "from_value": "In Progress",
        "to_value": "Closed",
        "changed_at": "2026-07-03 09:00:00",
    },
]


def _entity(org_id: str, ci_id: str = "ci-server-001") -> Entity:
    return Entity(
        org_id=org_id,
        entity_type="system",
        canonical_name="application server",
        display_name="Application Server",
        source_system="servicenow",
        source_record_id=ci_id,
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id="run-cmdb",
        last_seen_run_id="run-cmdb",
        metadata={
            "ci_class": "cmdb_ci_server",
            "lifecycle_state": "active",
            "source_updated_at": "2026-07-01 08:00:00",
            "source_url": f"https://acme.service-now.com/cmdb_ci/{ci_id}",
        },
    )


def _resolved_item(
    *,
    sys_id: str = "vi-001",
    path=None,
    vulnerability_class: str = "Missing Patch",
) -> dict:
    history = deepcopy(_HISTORY if path is None else path)
    return {
        "sys_id": sys_id,
        "number": sys_id.upper(),
        "vulnerability_class": vulnerability_class,
        "state": "Closed",
        "state_history": history,
        "cmdb_ci": "host-prod-17",
        "source_timestamp": "2026-07-03 10:00:00",
        "scan_run_id": "scan-volatile-123",
        "cve_id": "CVE-2026-99999",
        "resolved_ci": {
            "ci_class": "cmdb_ci_server",
            "source_record_id": "host-prod-17",
            "display_name": "prod-web-host-17",
        },
    }


def test_signature_is_repeatable_versioned_and_normalized():
    first = compute_remediation_signature(
        vulnerability_class="  Missing   PATCH ",
        ci_class=" CMDB_CI_SERVER ",
        remediation_path=" Detected -> Assigned -> In   Progress -> Closed ",
    )
    second = compute_remediation_signature(
        vulnerability_class="missing patch",
        ci_class="cmdb_ci_server",
        remediation_path=["detected", "assigned", "in progress", "closed"],
    )

    assert first == second
    assert first == compute_remediation_signature(
        vulnerability_class="missing patch",
        ci_class="cmdb_ci_server",
        remediation_path=["detected", "assigned", "in progress", "closed"],
    )
    assert first.startswith(f"{REMEDIATION_SIGNATURE_VERSION}:")
    digest = first.split(":", 1)[1]
    assert len(digest) == 32
    int(digest, 16)
    assert normalize_remediation_path(
        ["Detected", " detected ", "ASSIGNED", " In   Progress "]
    ) == ("detected", "assigned", "in progress")


def test_transition_response_order_does_not_change_the_remediation_path():
    expected = ("detected", "assigned", "in progress", "closed")
    assert remediation_path_from_history(_HISTORY, current_state="Closed") == expected
    assert remediation_path_from_history(
        list(reversed(_HISTORY)), current_state=" closed "
    ) == expected


def test_near_miss_remediation_paths_never_collapse():
    fixed = compute_remediation_signature(
        vulnerability_class="missing patch",
        ci_class="cmdb_ci_server",
        remediation_path=["detected", "assigned", "in progress", "closed"],
    )
    deferred = compute_remediation_signature(
        vulnerability_class="missing patch",
        ci_class="cmdb_ci_server",
        remediation_path=["detected", "assigned", "deferred", "closed"],
    )
    assert fixed != deferred
    assert fixed != compute_remediation_signature(
        vulnerability_class="weak configuration",
        ci_class="cmdb_ci_server",
        remediation_path=["detected", "assigned", "in progress", "closed"],
    )
    assert fixed != compute_remediation_signature(
        vulnerability_class="missing patch",
        ci_class="cmdb_ci_db_instance",
        remediation_path=["detected", "assigned", "in progress", "closed"],
    )


def test_volatile_item_host_scan_and_timestamp_values_never_enter_the_signature():
    first = _resolved_item(sys_id="vi-first")
    second = _resolved_item(sys_id="vi-second")
    second.update(
        {
            "number": "VIT9999",
            "cmdb_ci": "host-prod-99",
            "source_timestamp": "2030-01-01 00:00:00",
            "scan_run_id": "different-scan",
            "cve_id": "CVE-2030-00001",
        }
    )
    second["resolved_ci"].update(
        {
            "source_record_id": "host-prod-99",
            "display_name": "another-host-name",
        }
    )

    assert apply_remediation_signature(first) == apply_remediation_signature(second)
    assert first["remediation_signature_components"] == {
        "version": REMEDIATION_SIGNATURE_VERSION,
        "recipe": ["vulnerability_class", "ci_class", "remediation_path"],
        "vulnerability_class": "missing patch",
        "ci_class": "cmdb_ci_server",
        "remediation_path": ["detected", "assigned", "in progress", "closed"],
    }
    assert REMEDIATION_SIGNATURE_INCLUDED_FIELDS == (
        "vulnerability_class",
        "ci_class",
        "remediation_path",
    )
    assert {
        "sys_id",
        "cmdb_ci",
        "host_name",
        "cve_id",
        "source_timestamp",
        "scan_run_id",
        "run_id",
    }.issubset(REMEDIATION_SIGNATURE_EXCLUDED_FIELDS)


def test_workload_aggregation_is_order_independent_and_class_level_only():
    fixed_one = _resolved_item(sys_id="vi-fixed-1")
    fixed_two = _resolved_item(sys_id="vi-fixed-2")
    deferred_history = [
        _HISTORY[0],
        {
            "field": "state",
            "from_value": "Assigned",
            "to_value": "Deferred",
            "changed_at": "2026-07-02 09:00:00",
        },
    ]
    deferred = _resolved_item(sys_id="vi-deferred", path=deferred_history)
    deferred["state"] = "Deferred"
    items = [fixed_one, deferred, fixed_two]

    summary = apply_remediation_signatures(items)
    reversed_summary = apply_remediation_signatures(deepcopy(list(reversed(items))))

    assert summary == reversed_summary
    assert summary["total_items"] == 3
    assert summary["signed_items"] == 3
    assert summary["unsigned_items"] == 0
    assert summary["pattern_count"] == 2
    assert [pattern["item_count"] for pattern in summary["patterns"]] == [2, 1]
    assert {tuple(pattern["remediation_path"]) for pattern in summary["patterns"]} == {
        ("detected", "assigned", "in progress", "closed"),
        ("detected", "assigned", "deferred"),
    }

    serialized = json.dumps(summary, sort_keys=True)
    for forbidden_value in (
        "vi-fixed-1",
        "vi-fixed-2",
        "vi-deferred",
        "host-prod-17",
        "prod-web-host-17",
        "CVE-2026-99999",
        "scan-volatile-123",
    ):
        assert forbidden_value not in serialized
    assert set(summary["patterns"][0]) == {
        "remediation_signature",
        "vulnerability_class",
        "ci_class",
        "remediation_path",
        "item_count",
    }


def test_ci_resolution_stamps_signatures_and_keeps_near_misses_in_separate_groups():
    org_id = "org-remediation-signature"
    entity = _entity(org_id, "ci-server-001")
    fixed = {
        **_resolved_item(sys_id="vi-fixed"),
        "org_id": org_id,
        "cmdb_ci": "ci-server-001",
        "source_type": "servicenow_vulnerable_item",
        "source_url": "https://acme.service-now.com/vi-fixed",
    }
    fixed.pop("resolved_ci")
    deferred = {
        **deepcopy(fixed),
        "sys_id": "vi-deferred",
        "number": "VI-DEFERRED",
        "state": "Deferred",
        "state_history": [
            _HISTORY[0],
            {
                "field": "state",
                "from_value": "Assigned",
                "to_value": "Deferred",
                "changed_at": "2026-07-02 09:00:00",
            },
        ],
    }
    unresolved = {
        **deepcopy(fixed),
        "sys_id": "vi-unresolved",
        "number": "VI-UNRESOLVED",
        "cmdb_ci": "ci-not-admitted",
    }
    metrics = {
        "org_id": org_id,
        "vulnerable_items": [fixed, deferred, unresolved],
    }

    counts = resolve_vulnerable_item_ci_references(
        org_id=org_id,
        vulnerable_item_metrics=metrics,
        cmdb_entities=[entity],
    )

    assert counts == {"resolved": 2, "unresolved": 1}
    assert fixed["remediation_signature"]
    assert deferred["remediation_signature"]
    assert fixed["remediation_signature"] != deferred["remediation_signature"]
    assert unresolved["remediation_signature"] is None
    assert unresolved["remediation_signature_reason"] == "missing_resolved_ci_class"
    assert metrics["remediation_workload_summary"]["pattern_count"] == 2
    assert metrics["remediation_workload_summary"]["unsigned_items"] == 1


def test_component_audit_function_uses_only_the_documented_recipe():
    components = remediation_signature_components(
        vulnerability_class=" Missing Patch ",
        ci_class=" CMDB_CI_SERVER ",
        remediation_path=["Detected", "Assigned", "Closed"],
    )
    assert components == {
        "version": REMEDIATION_SIGNATURE_VERSION,
        "recipe": ["vulnerability_class", "ci_class", "remediation_path"],
        "vulnerability_class": "missing patch",
        "ci_class": "cmdb_ci_server",
        "remediation_path": ["detected", "assigned", "closed"],
    }
