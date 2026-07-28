"""MSP-B4 T5 / AT-666 — soft-join enrichment contract tests.

Covers both joined and unjoined scenarios for the two soft enrichment paths a
recurrence finding carries:

* CI-location join (B3): recurrence located to a CI (with the incident→CI
  provenance hop); recurrence WITHOUT B3 still emitted, unlocated; and the
  distinct "join failed" case (B3 present, CI reference does not resolve).
* Event-signature join (B0/B7): recurrence linked to an explicit event
  signature; recurrence with no explicit link still emitted, unlinked; and NO
  speculative link from timing- or text-similar values.
"""
from __future__ import annotations

import json

from app.provenance import EvidencePointer
from discovery.detectors.ops_recurrence import RecurrenceConfig, detect, find_recurrences
from discovery.detectors.ops_recurrence_joins import (
    STATUS_JOINED,
    STATUS_JOIN_FAILED,
    STATUS_NOT_AVAILABLE,
)
from discovery.signals.event_signature import compute_event_signature

_AS_OF = "2026-07-15 12:00:00"
_CI_SYS_ID = "ci-app-service-001"
_CI_CLASS = "cmdb_ci_service_auto"
_EVENT_SIGNATURE = compute_event_signature(
    source_system="aws_cloudwatch",
    event_class="state_change",
    resource_type="compute",
    event_type="ALARM State Change",
    resource_id="i-0abc",
)


def _incident(
    number: int,
    *,
    org_id: str = "org-a",
    ci_ref: str | None = _CI_SYS_ID,
    affected_ci: str | None = None,
    event_signatures: list[str] | None = None,
    resolved_at: str = "2026-07-10 12:00:00",
) -> dict:
    sys_id = f"incident-sys-{number:04d}"
    evidence = EvidencePointer.observed(
        source_system="servicenow",
        source_artifact=sys_id,
        source_timestamp=resolved_at,
        source_artifact_type="record_id",
    ).to_dict()
    incident: dict = {
        "sys_id": sys_id,
        "number": f"INC{number:07d}",
        "org_id": org_id,
        "category": "software",
        "close_code": "Solved (Permanently)",
        "assignment_group": "Platform Operations",
        "short_description": "Portal email service unavailable",
        "resolved_at": resolved_at,
        # Person-level noise that must never leak into join output (AC4).
        "assigned_to": "Alice Example",
        "resolved_by": "Carol Example",
        "resolution": {
            "is_resolved": True,
            "resolution_category": "software",
            "close_code": "Solved (Permanently)",
            "resolved_by_group": "Platform Operations",
            "resolved_at": resolved_at,
            "time_to_resolve_seconds": 3600,
            "evidence": evidence,
        },
    }
    if ci_ref is not None:
        incident["cmdb_ci"] = {"value": ci_ref, "display_value": "Portal"}
    if affected_ci is not None:
        incident["affected_ci_references"] = [
            {
                "relationship_sys_id": f"rel-{number}",
                "incident_sys_id": sys_id,
                "ci_sys_id": affected_ci,
                "source_type": "servicenow_task_ci",
                "origin": "observed",
            }
        ]
    if event_signatures is not None:
        incident["event_signatures"] = event_signatures
    return incident


def _cmdb(*items: dict, org_id: str = "org-a") -> dict:
    return {"org_id": org_id, "configuration_items": list(items)}


def _ci_item(sys_id: str = _CI_SYS_ID, ci_class: str = _CI_CLASS) -> dict:
    return {
        "sys_id": sys_id,
        "name": "Citizen Services Portal",
        "ci_class": ci_class,
        "source_url": f"https://acme.service-now.com/cmdb_ci.do?sys_id={sys_id}",
        "updated_at": "2026-07-01 10:00:00",
    }


def _payload(*incidents: dict, org_id: str = "org-a", cmdb: dict | None = None) -> dict:
    data = {
        "org_id": org_id,
        "incident_metrics": {"org_id": org_id, "incidents": list(incidents)},
    }
    if cmdb is not None:
        data["cmdb"] = cmdb
    return data


def _three(ci_ref: str | None = _CI_SYS_ID, **kw) -> list[dict]:
    return [
        _incident(1, ci_ref=ci_ref, resolved_at="2026-07-10 12:00:00", **kw),
        _incident(2, ci_ref=ci_ref, resolved_at="2026-07-12 12:00:00", **kw),
        _incident(3, ci_ref=ci_ref, resolved_at="2026-07-14 12:00:00", **kw),
    ]


def _find(*incidents: dict, cmdb: dict | None = None, org_id: str = "org-a"):
    return find_recurrences(
        _payload(*incidents, org_id=org_id, cmdb=cmdb),
        config=RecurrenceConfig(floor=3, window_days=30),
        as_of=_AS_OF,
        org_id=org_id,
    )


def _hop(record, hop_name: str) -> dict:
    return next(h for h in record.evidence_trace["hops"] if h["hop"] == hop_name)


# ── AC5: CI-location join, with and without B3 ───────────────────────────────

def test_recurrence_located_to_ci_carries_incident_to_ci_provenance():
    record = _find(*_three(), cmdb=_cmdb(_ci_item()))[0]

    ci = record.ci_location
    assert ci["status"] == STATUS_JOINED
    assert ci["located"] is True
    assert ci["ci_class"] == _CI_CLASS
    assert ci["ci_id"] == _CI_SYS_ID
    assert ci["located_incident_count"] == 3
    assert ci["configuration_items"][0]["incident_count"] == 3
    # The incident→CI hop resolves to the CI record.
    pointer = ci["configuration_items"][0]["evidence"]
    assert EvidencePointer.from_dict(pointer).is_valid()
    assert pointer["source_artifact"] == _CI_SYS_ID
    # Evidence trace shows the hop landed.
    assert _hop(record, "incident_to_ci")["status"] == STATUS_JOINED
    assert record.evidence_trace["located"] is True


def test_recurrence_without_b3_still_emits_unlocated():
    # No cmdb block at all — the B3 soft dependency is absent (AC5).
    record = _find(*_three())[0]

    assert record.recurrence_count == 3  # still emitted
    ci = record.ci_location
    assert ci["status"] == STATUS_NOT_AVAILABLE
    assert ci["located"] is False
    assert ci["reason"] == "b3_cmdb_absent"
    assert ci["ci_class"] is None
    assert record.evidence_trace["located"] is False


def test_b3_present_but_no_ci_reference_is_not_available_not_failed():
    # CMDB present, but the incidents name no CI — nothing to locate against.
    record = _find(*_three(ci_ref=None), cmdb=_cmdb(_ci_item()))[0]

    ci = record.ci_location
    assert ci["status"] == STATUS_NOT_AVAILABLE
    assert ci["reason"] == "no_ci_reference"
    assert ci["located"] is False


def test_b3_present_but_ci_reference_unresolved_is_join_failed():
    # CMDB present, incidents name a CI that is NOT in the scoped CMDB — the
    # join was attempted and did not land (distinct from "not available").
    record = _find(*_three(ci_ref="ci-does-not-exist"), cmdb=_cmdb(_ci_item()))[0]

    ci = record.ci_location
    assert ci["status"] == STATUS_JOIN_FAILED
    assert ci["reason"] == "unresolved_ci_reference"
    assert ci["located"] is False
    assert record.recurrence_count == 3  # still emitted


def test_affected_ci_reference_is_the_documented_fallback_ci_source():
    # No primary cmdb_ci, but the affected-CI list points at a resolvable CI.
    incidents = [
        _incident(1, ci_ref=None, affected_ci=_CI_SYS_ID, resolved_at="2026-07-10 12:00:00"),
        _incident(2, ci_ref=None, affected_ci=_CI_SYS_ID, resolved_at="2026-07-12 12:00:00"),
        _incident(3, ci_ref=None, affected_ci=_CI_SYS_ID, resolved_at="2026-07-14 12:00:00"),
    ]
    record = _find(*incidents, cmdb=_cmdb(_ci_item()))[0]

    assert record.ci_location["status"] == STATUS_JOINED
    assert record.ci_location["ci_class"] == _CI_CLASS


# ── event-signature join, with and without an explicit upstream link ─────────

def test_recurrence_linked_to_event_signature_completes_the_loop():
    incidents = _three(event_signatures=[_EVENT_SIGNATURE])
    record = _find(*incidents, cmdb=_cmdb(_ci_item()))[0]

    link = record.event_signature_link
    assert link["status"] == STATUS_JOINED
    assert link["linked"] is True
    assert link["event_signatures"] == [_EVENT_SIGNATURE]
    assert link["linked_incident_count"] == 3
    loop = link["event_links"][0]
    assert loop["event_signature"] == _EVENT_SIGNATURE
    assert loop["incident_count"] == 3
    # The loop carries the incident → resolution evidence.
    assert loop["incidents"][0]["resolution_signature"] == record.resolution_signature
    assert EvidencePointer.from_dict(loop["incidents"][0]["evidence"]).is_valid()
    assert record.evidence_trace["event_linked"] is True


def test_recurrence_without_event_link_still_emits_unlinked():
    record = _find(*_three(), cmdb=_cmdb(_ci_item()))[0]

    link = record.event_signature_link
    assert link["status"] == STATUS_NOT_AVAILABLE
    assert link["reason"] == "no_event_link"
    assert link["linked"] is False
    assert record.recurrence_count == 3
    assert record.evidence_trace["event_linked"] is False


def test_no_speculative_event_link_from_non_signature_text():
    # A value that is not a deterministic event_signature (free text / a bare
    # timestamp / a similar-looking string) must NEVER be treated as a link.
    junk = ["portal outage", "2026-07-10T12:00:00Z", "1:not-a-real-hex-digest"]
    record = _find(*_three(event_signatures=junk), cmdb=_cmdb(_ci_item()))[0]

    assert record.event_signature_link["status"] == STATUS_NOT_AVAILABLE
    assert record.event_signature_link["event_signatures"] == []


# ── evidence trace shape, determinism, privacy, org-scope ────────────────────

def test_evidence_trace_lists_all_three_hops_in_loop_order():
    record = _find(*_three(event_signatures=[_EVENT_SIGNATURE]), cmdb=_cmdb(_ci_item()))[0]

    hops = [h["hop"] for h in record.evidence_trace["hops"]]
    assert hops == [
        "recurrence_to_incident",
        "incident_to_ci",
        "event_signature_to_incident_to_resolution",
    ]
    recurrence_hop = _hop(record, "recurrence_to_incident")
    assert recurrence_hop["status"] == "present"
    assert recurrence_hop["incident_count"] == 3
    assert len(recurrence_hop["example_evidence_pointers"]) == 3


def test_missing_soft_joins_distinguish_not_available_from_failed():
    # not_available (no B3) vs join_failed (B3 present, CI unresolved) are two
    # different statuses — a consumer can tell them apart.
    not_available = _find(*_three())[0].ci_location["status"]
    failed = _find(*_three(ci_ref="ci-missing"), cmdb=_cmdb(_ci_item()))[0].ci_location["status"]
    assert not_available == STATUS_NOT_AVAILABLE
    assert failed == STATUS_JOIN_FAILED
    assert not_available != failed


def test_joins_are_deterministic_under_input_reordering():
    incidents = _three(event_signatures=[_EVENT_SIGNATURE])
    cmdb = _cmdb(_ci_item())
    forward = _find(*incidents, cmdb=cmdb)[0].as_dict()
    reverse = _find(*reversed(incidents), cmdb=cmdb)[0].as_dict()
    assert forward["evidence_trace"] == reverse["evidence_trace"]
    assert forward["ci_location"] == reverse["ci_location"]
    assert forward["event_signature_link"] == reverse["event_signature_link"]


def test_join_output_names_no_individuals():
    incidents = _three(event_signatures=[_EVENT_SIGNATURE])
    record = _find(*incidents, cmdb=_cmdb(_ci_item()))[0]
    rendered = json.dumps(
        {
            "ci_location": record.ci_location,
            "event_signature_link": record.event_signature_link,
            "evidence_trace": record.evidence_trace,
        }
    )
    for forbidden in ("Alice Example", "Carol Example", "assigned_to", '"resolved_by"'):
        assert forbidden not in rendered


def test_ci_join_is_org_scoped():
    # A CMDB block for a different org must not locate this org's recurrence.
    record = _find(*_three(), cmdb=_cmdb(_ci_item(), org_id="org-b"))[0]
    assert record.ci_location["status"] == STATUS_NOT_AVAILABLE
    assert record.ci_location["reason"] == "b3_cmdb_absent"


def test_joins_survive_the_detect_entrypoint():
    incidents = _three(event_signatures=[_EVENT_SIGNATURE])
    data = _payload(*incidents, cmdb=_cmdb(_ci_item()))
    results = detect(
        {}, data, config=RecurrenceConfig(floor=3, window_days=30), as_of=_AS_OF
    )
    assert len(results) == 1
    evidence = results[0].raw_evidence
    assert evidence["ci_location"]["status"] == STATUS_JOINED
    assert evidence["event_signature_link"]["status"] == STATUS_JOINED
    assert len(evidence["evidence_trace"]["hops"]) == 3
