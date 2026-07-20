"""MSP-B4 / AT-668 (T7) — the Resolution-Loop Depth contract suite (Section 3).

This is the consolidated contract for MSP-B4: the deterministic recurrence and
reassignment ping-pong guarantees that turn "lots of incidents" into "this
specific loop, N times, measured". Each test is labelled with the Section-3
acceptance criterion it discharges and reproduces that criterion's scenario as
stated in the story.

  * AC1 — seeded incidents with matching category/CI/close-code recurring above
          the floor produce a RecurrenceRecord with correct count, window,
          median time-to-resolve, and a resolvable example evidence set.  (T3)
  * AC2 — near-miss incidents (same category, different close code; same close
          code, different CI class) do NOT group — deterministic-signature
          separation from both sides.                                    (T2/T3)
  * AC3 — a seeded A→B→A→B assignment history is flagged with hop count and
          groups; a normal single escalation A→B is not.                 (T4)
  * AC4 — ping-pong and recurrence findings reference assignment groups and
          queues only — no individual is named in any detector output.   (T3/T4)
  * AC5 — with B3 present, a recurrence located to a CI carries the incident→CI
          provenance hop; without B3 it still emits, unlocated — and the
          B0/B7 event-signature join is the same soft story.             (T5)
  * AC6 — seeded credentials in resolution notes never appear in retrievable
          content (redaction-before-indexing), while the note remains reachable
          via the evidence pointer under access control.                 (T6)
  * AC7 — all reads read-only and org-scoped; a two-org test proves findings and
          evidence do not cross tenant boundaries.                       (T1/T3/T4/T6)

Pure-Python and offline — no ServiceNow credentials and no contract DB — so it
runs alongside the other MSP signal tests.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from app.provenance import EvidencePointer  # noqa: E402
from discovery.detectors.ops_pingpong import (  # noqa: E402
    DETECTOR_ID as PINGPONG_DETECTOR_ID,
    find_ping_pong,
)
from discovery.detectors.ops_pingpong import detect as pingpong_detect  # noqa: E402
from discovery.detectors.ops_recurrence import (  # noqa: E402
    DETECTOR_ID as RECURRENCE_DETECTOR_ID,
    RecurrenceConfig,
    detect as recurrence_detect,
    find_recurrences,
)
from discovery.detectors.ops_recurrence_joins import (  # noqa: E402
    STATUS_JOINED,
    STATUS_JOIN_FAILED,
    STATUS_NOT_AVAILABLE,
)
from discovery.ingest.servicenow_notes_handoff import (  # noqa: E402
    RESOLUTION_NOTE_CONNECTOR_ID,
    build_resolution_note_artifact,
    ingest_resolution_notes,
)
from discovery.signals.event_signature import compute_event_signature  # noqa: E402
from discovery.signals.resolution_signature import (  # noqa: E402
    compute_incident_identity_signature,
    compute_resolution_signature,
)

_AS_OF = "2026-07-15 12:00:00"
_CONFIG = RecurrenceConfig(floor=3, window_days=30, max_examples=3)

# Person-level values that resemble real ServiceNow payload noise. Not one of
# these individual names may ever appear in a detector's output (AC4).
_INDIVIDUALS = (
    "Alice Example", "Bob Example", "Carol Example",
    "Dana Example", "Erin Example", "Frank Example",
)

# A spread of real secret signatures the shared R18-A2 scanner recognises (AC6).
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_GH_TOKEN = "ghp_" + "a" * 36
_PASSWORD_ASSIGN = "password=Sup3rSecretValue"
_CONN_STRING = "Server=db;Uid=admin;Password=Hunter2Hunter2;"

_EVENT_SIGNATURE = compute_event_signature(
    source_system="aws_cloudwatch",
    event_class="state_change",
    resource_type="compute",
    event_type="ALARM State Change",
    resource_id="i-0abc",
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures builders — deterministic, self-contained incident payloads.
# ─────────────────────────────────────────────────────────────────────────────


def _incident(
    number: int,
    *,
    org_id: str = "org-a",
    category: str = "software",
    close_code: str = "Solved (Permanently)",
    ci_class: str = "cmdb_ci_server",
    ci_ref: str | None = None,
    affected_ci: str | None = None,
    group: str = "Platform Operations",
    short_description: str = "Portal email service unavailable",
    resolved_at: str = "2026-07-10 12:00:00",
    ttr: int | None = 3600,
    note: str = "",
    event_signatures: list[str] | None = None,
) -> dict:
    sys_id = f"incident-sys-{number:04d}"
    identity_signature = compute_incident_identity_signature(
        category=category, short_description=short_description, ci_class=ci_class
    )
    resolution_signature = compute_resolution_signature(
        category=category, close_code=close_code,
        resolved_by_group=group, ci_class=ci_class,
    )
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
        "category": category,
        "ci_class": ci_class,
        "short_description": short_description,
        "assignment_group": group,
        "close_code": close_code,
        "resolved_at": resolved_at,
        "close_notes": note,
        # Person-level noise the detectors must never copy into their output.
        "assigned_to": "Alice Example",
        "caller_id": "Bob Example",
        "resolved_by": "Carol Example",
        "source_url": (
            "https://acme.service-now.com/nav_to.do?"
            f"uri=incident.do%3Fsys_id%3D{sys_id}"
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
            "incident_sys_id": sys_id,
            "evidence": evidence,
            "notes_evidence": dict(evidence),
        },
    }
    if ci_ref is not None:
        incident["cmdb_ci"] = {"value": ci_ref, "display_value": "Portal"}
    if affected_ci is not None:
        incident["affected_ci_references"] = [
            {"ci_sys_id": affected_ci, "incident_sys_id": sys_id, "origin": "observed"}
        ]
    if event_signatures is not None:
        incident["event_signatures"] = event_signatures
    return incident


def _payload(*incidents: dict, org_id: str = "org-a", cmdb: dict | None = None) -> dict:
    data = {
        "org_id": org_id,
        "incident_metrics": {"org_id": org_id, "incidents": list(incidents)},
    }
    if cmdb is not None:
        data["cmdb"] = cmdb
    return data


def _three_matching(**kw) -> list[dict]:
    return [
        _incident(1, resolved_at="2026-07-10 12:00:00", ttr=3600, **kw),
        _incident(2, resolved_at="2026-07-12 12:00:00", ttr=7200, **kw),
        _incident(3, resolved_at="2026-07-14 12:00:00", ttr=10800, **kw),
    ]


def _cmdb(sys_id: str, ci_class: str, *, org_id: str = "org-a") -> dict:
    return {
        "org_id": org_id,
        "configuration_items": [
            {
                "sys_id": sys_id,
                "name": "Citizen Services Portal",
                "ci_class": ci_class,
                "updated_at": "2026-07-01 10:00:00",
                "source_url": f"https://acme.service-now.com/cmdb_ci.do?sys_id={sys_id}",
            }
        ],
    }


def _history(*groups) -> list[dict]:
    return [
        {
            "assignment_group": group,
            "changed_at": f"2026-07-01 0{index + 1}:00:00",
            "history_sys_id": f"audit-{index + 1}",
            # Person-level audit noise — never emitted by the detector.
            "assigned_to": "Dana Example",
            "updated_by": "Erin Example",
        }
        for index, group in enumerate(groups)
    ]


def _routing_incident(sys_id: str, *groups, org_id: str = "org-a") -> dict:
    return {
        "sys_id": sys_id,
        "number": "INC9000001",
        "org_id": org_id,
        "assignment_history": _history(*groups),
        "source_url": f"https://acme.service-now.com/incident.do?sys_id={sys_id}",
        "assigned_to": "Frank Example",
        "caller_id": "Bob Example",
    }


class _CapturingIngest:
    """Fake retrieval-substrate entry point — records what it was handed."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, org_id, artifacts):
        self.calls.append((org_id, list(artifacts)))
        return {"org_id": org_id, "artifacts": len(artifacts)}


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — recurrence record: count, window, median TTR, resolvable examples.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC1RecurrenceRecord:
    def test_matching_incidents_above_floor_emit_one_complete_record(self):
        records = find_recurrences(
            _payload(*_three_matching()), config=_CONFIG, as_of=_AS_OF
        )
        assert len(records) == 1
        record = records[0]

        # Correct count.
        assert record.recurrence_count == 3
        assert record.count == 3
        assert record.recurrence_floor == 3

        # Correct evaluated window (30 days ending at as_of).
        assert record.evaluated_window == {
            "start": "2026-06-15T12:00:00+00:00",
            "end": "2026-07-15T12:00:00+00:00",
            "days": 30,
        }
        assert record.window == record.evaluated_window

        # Correct median time-to-resolve (median of 3600/7200/10800).
        assert record.median_time_to_resolve_seconds == 7200
        assert record.median_ttr == 7200
        assert record.measured_ttr_count == 3

        # A resolvable example evidence set.
        assert len(record.examples) == 3
        assert len(record.example_evidence_pointers) == 3
        assert all(
            EvidencePointer.from_dict(pointer).is_valid()
            for pointer in record.example_evidence_pointers
        )
        assert [ex["incident_sys_id"] for ex in record.examples] == [
            "incident-sys-0001", "incident-sys-0002", "incident-sys-0003",
        ]

    def test_below_floor_does_not_emit(self):
        two = _three_matching()[:2]
        assert find_recurrences(_payload(*two), config=_CONFIG, as_of=_AS_OF) == []

    def test_outside_window_does_not_count(self):
        incidents = _three_matching()
        incidents[0]["resolved_at"] = "2026-05-01 12:00:00"
        incidents[0]["resolution"]["resolved_at"] = "2026-05-01 12:00:00"
        records = find_recurrences(_payload(*incidents), config=_CONFIG, as_of=_AS_OF)
        # The May incident is outside the 30-day window → below floor → no record.
        assert records == []

    def test_detect_entrypoint_emits_observed_result(self):
        results = recurrence_detect(
            {}, _payload(*_three_matching()), config=_CONFIG, as_of=_AS_OF
        )
        assert len(results) == 1
        assert results[0].detector_id == RECURRENCE_DETECTOR_ID
        assert results[0].provenance_type == "observed"
        assert results[0].metric_value == 3


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — near-miss separation from both sides (deterministic signatures).
# ─────────────────────────────────────────────────────────────────────────────


class TestAC2NearMissSeparation:
    def test_same_category_different_close_code_does_not_group(self):
        near_miss = _incident(
            9, close_code="Solved (Work Around)", resolved_at="2026-07-14 13:00:00"
        )
        records = find_recurrences(
            _payload(*_three_matching(), near_miss), config=_CONFIG, as_of=_AS_OF
        )
        assert len(records) == 1
        assert records[0].recurrence_count == 3
        assert "incident-sys-0009" not in {
            ex["incident_sys_id"] for ex in records[0].examples
        }

    def test_same_close_code_different_ci_class_does_not_group(self):
        near_miss = _incident(
            8, ci_class="cmdb_ci_db_instance", resolved_at="2026-07-14 13:00:00"
        )
        records = find_recurrences(
            _payload(*_three_matching(), near_miss), config=_CONFIG, as_of=_AS_OF
        )
        assert len(records) == 1
        assert records[0].recurrence_count == 3

    def test_near_miss_signatures_differ_at_the_signature_layer(self):
        base = compute_resolution_signature(
            category="software", close_code="Solved (Permanently)",
            resolved_by_group="Platform Operations", ci_class="cmdb_ci_server",
        )
        diff_close = compute_resolution_signature(
            category="software", close_code="Solved (Work Around)",
            resolved_by_group="Platform Operations", ci_class="cmdb_ci_server",
        )
        diff_ci = compute_resolution_signature(
            category="software", close_code="Solved (Permanently)",
            resolved_by_group="Platform Operations", ci_class="cmdb_ci_db_instance",
        )
        assert base != diff_close
        assert base != diff_ci


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — A→B→A→B flagged with hop count + groups; normal A→B is not.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC3PingPong:
    def test_a_b_a_b_is_flagged_with_hop_count_and_groups(self):
        results = pingpong_detect(
            {}, _payload(_routing_incident("inc-1", "Group A", "Group B", "Group A", "Group B"))
        )
        assert len(results) == 1
        finding = results[0].raw_evidence
        assert results[0].detector_id == PINGPONG_DETECTOR_ID
        assert finding["hop_count"] == 3
        assert finding["groups_involved"] == ["Group A", "Group B"]
        assert finding["assignment_sequence"] == [
            "Group A", "Group B", "Group A", "Group B"
        ]

    def test_normal_single_escalation_is_not_flagged(self):
        data = _payload(_routing_incident("inc-normal", "Group A", "Group B"))
        assert pingpong_detect({}, data) == []

    def test_longer_oscillation_keeps_full_ordered_chain(self):
        finding = find_ping_pong(
            _payload(_routing_incident("inc-long", "Intake", "Net", "Intake", "Net", "Intake"))
        )[0]
        assert finding.hop_count == 4
        assert finding.assignment_sequence == ("Intake", "Net", "Intake", "Net", "Intake")


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — groups and queues only; no individual named in any detector output.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC4GroupsNeverPeople:
    def test_recurrence_output_names_no_individuals(self):
        incidents = _three_matching()
        incidents[0]["close_notes"] = "Alice Example used password=hunter2"
        record = find_recurrences(_payload(*incidents), config=_CONFIG, as_of=_AS_OF)[0]
        rendered = json.dumps(record.as_dict())
        for individual in _INDIVIDUALS:
            assert individual not in rendered
        for forbidden in ("assigned_to", "caller_id", '"resolved_by":', "close_notes"):
            assert forbidden not in rendered
        # The group/queue IS named — that is the process-friction signal.
        assert "resolved_by_assignment_group" in rendered

    def test_pingpong_output_names_no_individuals(self):
        incident = _routing_incident("inc-private", "Group A", "Group B", "Group A", "Group B")
        incident["work_notes"] = "Contact Frank Example for approval"
        rendered = json.dumps(pingpong_detect({}, _payload(incident))[0].raw_evidence)
        for individual in _INDIVIDUALS:
            assert individual not in rendered
        for forbidden in ("assigned_to", "caller_id", "updated_by", "work_notes"):
            assert forbidden not in rendered
        assert "Group A" in rendered and "Group B" in rendered


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — soft joins: B3 (CI location) and B0/B7 (event signature), both ways.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC5SoftJoins:
    def _hop(self, record, name):
        return next(h for h in record.evidence_trace["hops"] if h["hop"] == name)

    def test_with_b3_recurrence_located_carries_incident_to_ci_provenance(self):
        incidents = _three_matching(ci_ref="ci-portal-001")
        record = find_recurrences(
            _payload(*incidents, cmdb=_cmdb("ci-portal-001", "cmdb_ci_service_auto")),
            config=_CONFIG, as_of=_AS_OF,
        )[0]
        ci = record.ci_location
        assert ci["status"] == STATUS_JOINED
        assert ci["located"] is True
        assert ci["ci_class"] == "cmdb_ci_service_auto"
        assert ci["located_incident_count"] == 3
        pointer = ci["configuration_items"][0]["evidence"]
        assert EvidencePointer.from_dict(pointer).is_valid()
        assert pointer["source_artifact"] == "ci-portal-001"
        assert self._hop(record, "incident_to_ci")["status"] == STATUS_JOINED
        assert record.evidence_trace["located"] is True

    def test_without_b3_recurrence_still_emits_unlocated(self):
        record = find_recurrences(
            _payload(*_three_matching(ci_ref="ci-portal-001")), config=_CONFIG, as_of=_AS_OF
        )[0]
        assert record.recurrence_count == 3  # still emitted
        assert record.ci_location["status"] == STATUS_NOT_AVAILABLE
        assert record.ci_location["reason"] == "b3_cmdb_absent"
        assert record.evidence_trace["located"] is False

    def test_b3_present_but_ci_unresolved_is_join_failed_not_not_available(self):
        record = find_recurrences(
            _payload(
                *_three_matching(ci_ref="ci-missing"),
                cmdb=_cmdb("ci-portal-001", "cmdb_ci_service_auto"),
            ),
            config=_CONFIG, as_of=_AS_OF,
        )[0]
        assert record.ci_location["status"] == STATUS_JOIN_FAILED
        assert record.recurrence_count == 3  # still emitted

    def test_b0_b7_event_signature_join_completes_loop_when_present(self):
        incidents = _three_matching(
            ci_ref="ci-portal-001", event_signatures=[_EVENT_SIGNATURE]
        )
        record = find_recurrences(
            _payload(*incidents, cmdb=_cmdb("ci-portal-001", "cmdb_ci_service_auto")),
            config=_CONFIG, as_of=_AS_OF,
        )[0]
        link = record.event_signature_link
        assert link["status"] == STATUS_JOINED
        assert link["event_signatures"] == [_EVENT_SIGNATURE]
        assert link["linked_incident_count"] == 3
        assert record.evidence_trace["event_linked"] is True

    def test_no_event_link_still_emits_unlinked(self):
        record = find_recurrences(
            _payload(*_three_matching()), config=_CONFIG, as_of=_AS_OF
        )[0]
        assert record.event_signature_link["status"] == STATUS_NOT_AVAILABLE
        assert record.event_signature_link["reason"] == "no_event_link"
        assert record.evidence_trace["event_linked"] is False

    def test_no_speculative_event_link_from_non_signature_text(self):
        junk = ["portal outage", "2026-07-10T12:00:00Z", "1:not-a-real-hex-digest"]
        record = find_recurrences(
            _payload(*_three_matching(event_signatures=junk)), config=_CONFIG, as_of=_AS_OF
        )[0]
        assert record.event_signature_link["status"] == STATUS_NOT_AVAILABLE
        assert record.event_signature_link["event_signatures"] == []


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — seeded credentials in notes never reach retrievable content; the note
#       stays reachable via the evidence pointer under access control.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC6RedactionBeforeIndexing:
    @pytest.mark.parametrize("secret", [_AWS_KEY, _GH_TOKEN, _PASSWORD_ASSIGN, _CONN_STRING])
    def test_seeded_secret_absent_from_retrievable_content(self, secret):
        ingest = _CapturingIngest()
        incident = _incident(1, note=f"Rotated {secret}. See runbook KB0010234.")
        ingest_resolution_notes(
            "org-a", [incident], ingest_fn=ingest, record_event_fn=lambda *a: None
        )
        content = ingest.calls[0][1][0].content
        for fragment in ("AKIAIOSFODNN7EXAMPLE", "ghp_", "Sup3rSecretValue", "Hunter2Hunter2"):
            assert fragment not in content, f"secret leaked into retrieval content: {fragment}"
        assert "[REDACTED:" in content

    def test_useful_content_survives_redaction(self):
        ingest = _CapturingIngest()
        incident = _incident(1, note=f"Restarted service; {_AWS_KEY} rotated. Per KB0010234.")
        ingest_resolution_notes("org-a", [incident], ingest_fn=ingest)
        content = ingest.calls[0][1][0].content
        assert "Restarted service" in content
        assert "KB0010234" in content

    def test_note_remains_reachable_via_evidence_pointer(self):
        built = build_resolution_note_artifact(_incident(1, note=f"secret {_AWS_KEY}"))
        assert built is not None
        artifact = built[0]
        pointer = artifact.provenance["evidence_pointer"]
        assert pointer["source_system"] == "servicenow"
        assert pointer["source_artifact"] == "incident-sys-0001"
        assert pointer["origin"] == "observed"
        # The access-controlled source path travels with the artifact too.
        assert "service-now.com" in artifact.provenance["source_url"]

    def test_redaction_uses_the_single_r18a2_path(self):
        import discovery.ingest.servicenow_notes_handoff as mod

        assert mod.scan_and_redact.__module__ == "discovery.ingest.secret_redaction"

    def test_redaction_event_is_recorded_without_the_secret_value(self):
        events: list = []
        ingest_resolution_notes(
            "org-a",
            [_incident(1, note=f"leak {_AWS_KEY} and {_PASSWORD_ASSIGN}")],
            ingest_fn=_CapturingIngest(),
            record_event_fn=lambda t, p: events.append((t, p)),
        )
        assert len(events) == 1
        event_type, payload = events[0]
        assert event_type == "ingestion.secret_redacted"
        assert payload["connector_id"] == RESOLUTION_NOTE_CONNECTOR_ID
        blob = str(payload)
        assert "AKIAIOSFODNN7EXAMPLE" not in blob
        assert "Sup3rSecretValue" not in blob


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — reads read-only and org-scoped; a two-org test proves no cross-tenant
#       leakage of findings or evidence.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC7ReadOnlyAndOrgScoped:
    def test_servicenow_client_exposes_no_write_methods(self):
        from discovery.ingest.servicenow import ServiceNowClient

        for verb in ("post", "put", "patch", "delete", "insert", "update", "write"):
            assert not hasattr(ServiceNowClient, verb), verb

    def test_recurrence_never_counts_another_orgs_incident(self):
        org_a = _three_matching()
        org_b = _incident(99, org_id="org-b", resolved_at="2026-07-14 13:00:00")
        records = find_recurrences(
            _payload(*org_a, org_b, org_id="org-a"),
            config=_CONFIG, as_of=_AS_OF, org_id="org-a",
        )
        assert len(records) == 1
        assert records[0].org_id == "org-a"
        assert records[0].recurrence_count == 3
        assert all(
            ex["incident_sys_id"] != "incident-sys-0099" for ex in records[0].examples
        )

    def test_mixed_org_input_without_scope_fails_closed(self):
        incidents = _three_matching()
        incidents.append(_incident(99, org_id="org-b", resolved_at="2026-07-14 13:00:00"))
        with pytest.raises(ValueError, match="org_id is required"):
            find_recurrences(
                {"incident_metrics": {"incidents": incidents}},
                config=_CONFIG, as_of=_AS_OF,
            )

    def test_pingpong_never_flags_another_orgs_incident(self):
        org_a = _routing_incident("inc-a", "A1", "A2", "A1", org_id="org-a")
        org_b = _routing_incident("inc-b", "B1", "B2", "B1", org_id="org-b")
        findings = find_ping_pong(_payload(org_a, org_b, org_id="org-a"), org_id="org-a")
        assert [f.incident_sys_id for f in findings] == ["inc-a"]
        assert findings[0].org_id == "org-a"

    def test_ci_location_join_is_org_scoped(self):
        # A CMDB block belonging to another org must not locate this recurrence.
        record = find_recurrences(
            _payload(
                *_three_matching(ci_ref="ci-portal-001"),
                cmdb=_cmdb("ci-portal-001", "cmdb_ci_service_auto", org_id="org-b"),
            ),
            config=_CONFIG, as_of=_AS_OF, org_id="org-a",
        )[0]
        assert record.ci_location["status"] == STATUS_NOT_AVAILABLE

    def test_note_handoff_writes_only_the_named_orgs_partition(self):
        ingest = _CapturingIngest()
        ingest_resolution_notes(
            "org-a", [_incident(1, note=f"k {_AWS_KEY}")], ingest_fn=ingest
        )
        ingest_resolution_notes(
            "org-b", [_incident(2, note=f"k {_AWS_KEY}")], ingest_fn=ingest
        )
        assert ingest.calls[0][0] == "org-a"
        assert ingest.calls[1][0] == "org-b"
        assert ingest.calls[0][1][0].source_artifact == "incident-sys-0001"
        assert ingest.calls[1][1][0].source_artifact == "incident-sys-0002"

    def test_blank_org_on_note_handoff_is_rejected(self):
        with pytest.raises(ValueError):
            ingest_resolution_notes("", [_incident(1)], ingest_fn=_CapturingIngest())


# ─────────────────────────────────────────────────────────────────────────────
# Determinism — recurrence claims are only as trustworthy as their reproducibility.
# ─────────────────────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_recurrence_output_is_input_order_independent(self):
        incidents = _three_matching(ci_ref="ci-portal-001", event_signatures=[_EVENT_SIGNATURE])
        cmdb = _cmdb("ci-portal-001", "cmdb_ci_service_auto")
        forward = find_recurrences(
            _payload(*incidents, cmdb=cmdb), config=_CONFIG, as_of=_AS_OF
        )[0].as_dict()
        reverse = find_recurrences(
            _payload(*reversed(incidents), cmdb=cmdb), config=_CONFIG, as_of=_AS_OF
        )[0].as_dict()
        assert forward == reverse

    def test_pingpong_finding_id_is_stable(self):
        data = _payload(_routing_incident("inc-stable", "A Queue", "B Queue", "A Queue", "B Queue"))
        first = pingpong_detect({}, data)[0].raw_evidence
        second = pingpong_detect({}, data)[0].raw_evidence
        assert first == second
        assert first["finding_id"] == second["finding_id"]
