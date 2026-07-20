"""MSP-B4 T4 / AC3-AC4 — assignment-group ping-pong contract tests."""
from __future__ import annotations

import json


def _history(*groups, duplicate_first=False):
    values = list(groups)
    if duplicate_first and values:
        values.insert(1, values[0])
    return [
        {
            "assignment_group": group,
            "changed_at": f"2026-07-01 0{index + 1}:00:00",
            "history_sys_id": f"audit-{index + 1}",
            # These person-level values resemble real audit payload noise. The
            # detector must never copy either into a finding.
            "assigned_to": "Alice Example",
            "updated_by": "Bob Example",
        }
        for index, group in enumerate(values)
    ]


def _incident(sys_id, groups, *, org_id="org-a", number="INC001", **extra):
    return {
        "sys_id": sys_id,
        "number": number,
        "org_id": org_id,
        "source_url": (
            "https://acme.service-now.com/nav_to.do?"
            f"uri=incident.do%3Fsys_id%3D{sys_id}"
        ),
        "assignment_history": groups,
        "assigned_to": "Charlie Example",
        "caller_id": "Dana Example",
        **extra,
    }


def _payload(*incidents, org_id="org-a"):
    return {
        "org_id": org_id,
        "incident_metrics": {"org_id": org_id, "incidents": list(incidents)},
    }


def test_a_b_a_b_is_flagged_with_correct_hops_groups_and_evidence():
    from discovery.detectors.ops_pingpong import DETECTOR_ID, detect

    results = detect({}, _payload(_incident("inc-1", _history("Group A", "Group B", "Group A", "Group B"))))

    assert len(results) == 1
    result = results[0]
    assert result.detector_id == DETECTOR_ID
    assert result.metric_value == 3
    assert result.provenance_type == "observed"
    finding = result.raw_evidence
    assert finding["hop_count"] == 3
    assert finding["return_count"] == 2
    assert finding["groups_involved"] == ["Group A", "Group B"]
    assert finding["assignment_sequence"] == [
        "Group A", "Group B", "Group A", "Group B"
    ]
    assert finding["ownership_boundaries"] == [
        {"from_group": "Group A", "to_group": "Group B"},
        {"from_group": "Group B", "to_group": "Group A"},
        {"from_group": "Group A", "to_group": "Group B"},
    ]
    assert finding["incident_sys_id"] == "inc-1"
    assert finding["source_url"].endswith("inc-1")
    assert len(finding["evidence_pointers"]) == 4
    assert all(pointer["origin"] == "observed" for pointer in finding["evidence_pointers"])


def test_one_way_a_to_b_escalation_is_not_flagged():
    from discovery.detectors.ops_pingpong import detect, evaluate

    data = _payload(_incident("inc-normal", _history("Group A", "Group B")))
    assert detect({}, data) == []
    evaluation = evaluate({}, data)
    assert evaluation.fired is False
    assert evaluation.raw_evidence["ping_pong_incident_count"] == 0


def test_consecutive_duplicate_group_entries_are_not_counted_as_hops():
    from discovery.detectors.ops_pingpong import detect

    incident = _incident(
        "inc-duplicates",
        _history("Group A", "Group B", "Group B", "Group A", "Group B", duplicate_first=True),
    )
    finding = detect({}, _payload(incident))[0].raw_evidence

    assert finding["assignment_sequence"] == [
        "Group A", "Group B", "Group A", "Group B"
    ]
    assert finding["hop_count"] == 3


def test_longer_oscillation_preserves_the_complete_ordered_chain():
    from discovery.detectors.ops_pingpong import find_ping_pong

    incident = _incident(
        "inc-long",
        _history("Intake", "Network Queue", "Intake", "Network Queue", "Intake"),
    )
    finding = find_ping_pong(_payload(incident))[0]

    assert finding.hop_count == 4
    assert finding.groups_involved == ("Intake", "Network Queue")
    assert finding.assignment_sequence == (
        "Intake", "Network Queue", "Intake", "Network Queue", "Intake"
    )


def test_finding_output_contains_groups_not_individuals():
    from discovery.detectors.ops_pingpong import detect

    incident = _incident(
        "inc-private",
        _history("Group A", "Group B", "Group A", "Group B"),
        description="Escalated by Erin Example to Frank Example",
        work_notes="Contact Grace Example for approval",
    )
    rendered = json.dumps(detect({}, _payload(incident))[0].raw_evidence)

    for individual in (
        "Alice Example", "Bob Example", "Charlie Example", "Dana Example",
        "Erin Example", "Frank Example", "Grace Example",
    ):
        assert individual not in rendered
    for forbidden_key in ("assigned_to", "caller_id", "updated_by", "work_notes"):
        assert forbidden_key not in rendered
    assert "Group A" in rendered
    assert "Group B" in rendered


def test_same_input_produces_stable_finding_id_and_output():
    from discovery.detectors.ops_pingpong import detect

    data = _payload(_incident("inc-stable", _history("A Queue", "B Queue", "A Queue", "B Queue")))
    first = detect({}, data)[0].raw_evidence
    second = detect({}, data)[0].raw_evidence

    assert first == second
    assert first["finding_id"] == second["finding_id"]


def test_org_scope_excludes_another_organizations_incident():
    from discovery.detectors.ops_pingpong import find_ping_pong

    org_a = _incident("inc-a", _history("A1", "A2", "A1"), org_id="org-a")
    org_b = _incident("inc-b", _history("B1", "B2", "B1"), org_id="org-b")

    findings = find_ping_pong(_payload(org_a, org_b, org_id="org-a"), org_id="org-a")

    assert [finding.incident_sys_id for finding in findings] == ["inc-a"]
    assert findings[0].org_id == "org-a"


def test_offline_servicenow_payload_flows_directly_into_detector(monkeypatch):
    from discovery.detectors.ops_pingpong import detect
    from discovery.ingest import servicenow

    monkeypatch.setattr(servicenow, "is_live", lambda: False)
    data = servicenow.ingest(include_cmdb=False)
    results = detect({}, data)

    finding = next(result.raw_evidence for result in results if result.raw_evidence["incident_number"] == "INC0000005")
    assert finding["hop_count"] == 3
    assert finding["groups_involved"] == ["Level 1 Support", "Loan Operations"]


def test_live_history_read_is_bounded_ordered_group_only_and_read_only(monkeypatch):
    from discovery.ingest import servicenow

    calls = []

    class Client:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records=10_000):
            calls.append((table, dict(params), max_records))
            assert table == "sys_audit"
            assert params["sysparm_fields"] == ",".join(servicenow.ASSIGNMENT_HISTORY_FIELDS)
            assert "documentkeyINinc-1" in params["sysparm_query"]
            assert "fieldname=assignment_group" in params["sysparm_query"]
            for person_field in ("user", "assigned_to", "caller_id"):
                assert person_field not in params["sysparm_fields"]
            # Deliberately returned out of order: the contract must sort by
            # source timestamp and stable audit id.
            return [
                {
                    "sys_id": "audit-3",
                    "documentkey": "inc-1",
                    "oldvalue": "Group A",
                    "newvalue": "Group B",
                    "sys_created_on": "2026-07-01 03:00:00",
                },
                {
                    "sys_id": "audit-1",
                    "documentkey": "inc-1",
                    "oldvalue": "Group A",
                    "newvalue": "Group B",
                    "sys_created_on": "2026-07-01 01:00:00",
                },
                {
                    "sys_id": "audit-2",
                    "documentkey": "inc-1",
                    "oldvalue": "Group B",
                    "newvalue": "Group A",
                    "sys_created_on": "2026-07-01 02:00:00",
                },
            ]

    histories = servicenow.get_assignment_group_history(
        Client(), ["inc-1"], org_id="org-a"
    )

    assert len(calls) == 1
    assert [entry["assignment_group"] for entry in histories["inc-1"]] == [
        "Group A", "Group B", "Group A", "Group B"
    ]
    assert [entry["history_sys_id"] for entry in histories["inc-1"]] == [
        "audit-1", "audit-1", "audit-2", "audit-3"
    ]
    assert all(entry["org_id"] == "org-a" for entry in histories["inc-1"])
    assert all(entry["evidence"]["origin"] == "observed" for entry in histories["inc-1"])
    for write_method in ("post", "put", "patch", "delete", "insert", "update"):
        assert not hasattr(Client, write_method)


def test_audit_shaped_history_is_supported_without_person_fields():
    from discovery.detectors.ops_pingpong import detect

    incident = _incident(
        "inc-audit-shape",
        [
            {"oldvalue": "A", "newvalue": "B", "sys_created_on": "2026-07-01 01:00:00", "sys_id": "h1"},
            {"oldvalue": "B", "newvalue": "A", "sys_created_on": "2026-07-01 02:00:00", "sys_id": "h2"},
            {"oldvalue": "A", "newvalue": "B", "sys_created_on": "2026-07-01 03:00:00", "sys_id": "h3"},
        ],
    )

    finding = detect({}, _payload(incident))[0].raw_evidence
    assert finding["assignment_sequence"] == ["A", "B", "A", "B"]
    assert finding["hop_count"] == 3
