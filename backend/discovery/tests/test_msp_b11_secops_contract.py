"""MSP-B11 T7 / AT-702 — consolidated Security Operations contract suite.

One place that pins the combined SIR + Vulnerability Response behaviour against
every MSP-B11 Section-3 acceptance criterion (AC1–AC7). The per-task suites
(`test_msp_b11_sir_ingestion.py`, `test_msp_b11_vr_ingestion.py`,
`test_msp_b11_remediation_signatures.py`, `test_msp_b11_security_note_redaction.py`,
`test_msp_b11_vr_volume.py`) prove each task in depth; this suite is the
acceptance contract — it reproduces each AC's scenario end to end, across both
table families, and fails closed on the unresolved / cross-org cases.

Runs fully offline (deterministic fixtures + fakes); no live ServiceNow instance.
"""
from __future__ import annotations

import json
import logging
from dataclasses import fields
from datetime import datetime, timezone

import pytest

from app.vulnerable_item_ci_resolution import resolve_vulnerable_item_ci_references
from database.models.entities import Entity
from discovery.ingest import servicenow as sn
from discovery.ingest.base import Checkpoint
from discovery.ingest.change_runner import ingest_with_checkpoint
from discovery.ingest.secops_volume_harness import run_secops_volume_validation
from discovery.ingest.secret_redaction import scan_and_redact
from discovery.ingest.servicenow_security_notes_handoff import (
    build_security_note_artifact,
    ingest_security_notes,
)
from discovery.signals.budget import BudgetReport
from discovery.signals.ops_calibration import CALIBRATED_RUN_EVENT_BUDGET
from discovery.signals.remediation_signature import compute_remediation_signature
from discovery.signals.secops_volume import (
    TABLE_SECURITY_INCIDENT,
    TABLE_VULNERABLE_ITEM,
    SecOpsOrgScopeError,
    SecOpsVolumeStream,
)

# Watermark clocks: FIRST admits the early half of each fixture stream, NOW the rest.
FIRST_CLOCK = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

# Sensitive values seeded on the fixtures that must never cross the field scope.
_SEED_MARKERS = (
    "SEEDED OUT-OF-SCOPE", "hunter2", "CVE-2026-12345", "CVE-2026-55555", "CVSS:3.1",
)
_CVE_IDENTITY_FIELDS = {"vulnerability", "vulnerability_id", "cve", "cve_id", "cvss_vector"}


def _offline(monkeypatch) -> None:
    monkeypatch.setattr(sn, "is_live", lambda: False)


def _sir_keys() -> set:
    return {f.name for f in fields(sn.ServiceNowSecurityIncident)} | {"artifact_id", "change_kind"}


def _vi_keys() -> set:
    return {f.name for f in fields(sn.ServiceNowVulnerableItem)} | {"artifact_id", "change_kind"}


def _first_records(ingestor, org_id="org-a", since=None) -> list[dict]:
    return list(ingestor.ingest_changes(org_id, since))[0].records


def _checkpoint_store():
    stored: dict[tuple[str, str], Checkpoint] = {}

    def read_cp(org_id, connector_id):
        return stored.get((org_id, connector_id))

    def save_cp(cp):
        stored[(cp.org_id, cp.connector_id)] = cp

    return stored, read_cp, save_cp


# ═════════════════════════════════════════════════════════════════════════════
# AC1 — SIR + VR ingest as workflow signal; incremental on the second run
# ═════════════════════════════════════════════════════════════════════════════


def test_ac1_sir_ingests_workflow_signal_with_provenance(monkeypatch):
    _offline(monkeypatch)
    records = _first_records(
        sn.ServiceNowSecurityIncidentChangeIngestor(org_id="org-a", clock=lambda: NOW)
    )
    by_id = {r["sys_id"]: r for r in records}
    assert set(by_id) == {"sir-0001", "sir-0002", "sir-0003", "sir-0004"}

    inc = by_id["sir-0001"]
    # States, classifications, assignments, timestamps, transition history.
    assert inc["state"] and inc["category"] and inc["severity"]
    assert inc["assignment_group"] and inc["opened_at"]
    assert [(t["from_value"], t["to_value"]) for t in inc["state_history"]]
    assert [(t["from_value"], t["to_value"]) for t in inc["assignment_history"]]
    # Resolvable provenance.
    assert inc["org_id"] == "org-a"
    assert inc["origin"] == "observed"
    assert inc["source_type"] == sn.SIR_SOURCE_TYPE
    assert "sir-0001" in inc["source_url"]
    # A closed record carries close classification.
    assert by_id["sir-0003"]["close_code"] and by_id["sir-0003"]["resolution_code"]


def test_ac1_vr_ingests_items_groups_and_tasks_with_provenance(monkeypatch):
    _offline(monkeypatch)
    items = {r["sys_id"]: r for r in _first_records(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW))}
    groups = {r["sys_id"]: r for r in _first_records(
        sn.ServiceNowVulnerabilityGroupChangeIngestor(org_id="org-a", clock=lambda: NOW))}
    tasks = {r["sys_id"]: r for r in _first_records(
        sn.ServiceNowRemediationTaskChangeIngestor(org_id="org-a", clock=lambda: NOW))}

    assert set(items) == {f"vi-000{n}" for n in range(1, 7)}
    assert set(groups) == {"vg-0001", "vg-0002"}
    assert set(tasks) == {"rt-0001", "rt-0002"}

    vi = items["vi-0001"]
    assert vi["state"] == "In Progress"
    assert vi["vulnerability_class"] == "Missing Patch"
    assert vi["severity"] == "1 - Critical"
    assert vi["cmdb_ci"] == "ci-server-001"     # explicit CI ref for the T3 join
    assert vi["origin"] == "observed"
    assert vi["source_type"] == sn.VR_VULN_ITEM_SOURCE_TYPE
    # Deferral / exception classifications captured on the lifecycle.
    assert items["vi-0003"]["deferral_category"] == "Risk Accepted"
    assert items["vi-0006"]["exception_category"] == "False Positive"


def test_ac1_all_secops_streams_are_incremental_on_second_run(monkeypatch):
    _offline(monkeypatch)
    stored, read_cp, save_cp = _checkpoint_store()

    sir1 = sn.ingest_sir_changes(
        org_id="org-a", run_id="r1", clock=lambda: FIRST_CLOCK,
        read_checkpoint=read_cp, save_checkpoint=save_cp)
    vr1 = sn.ingest_vr_changes(
        org_id="org-a", run_id="r1", clock=lambda: FIRST_CLOCK,
        read_checkpoint=read_cp, save_checkpoint=save_cp)

    assert {i["sys_id"] for i in sir1["security_incidents"]} == {"sir-0001", "sir-0003"}
    assert {i["sys_id"] for i in vr1["vulnerable_items"]} == {
        "vi-0001", "vi-0002", "vi-0003", "vi-0004"}

    # Second run reads ONLY records updated after each stream's stored cursor.
    sir2 = sn.ingest_sir_changes(
        org_id="org-a", run_id="r2", clock=lambda: NOW,
        read_checkpoint=read_cp, save_checkpoint=save_cp)
    vr2 = sn.ingest_vr_changes(
        org_id="org-a", run_id="r2", clock=lambda: NOW,
        read_checkpoint=read_cp, save_checkpoint=save_cp)

    assert {i["sys_id"] for i in sir2["security_incidents"]} == {"sir-0002", "sir-0004"}
    assert {i["sys_id"] for i in vr2["vulnerable_items"]} == {"vi-0005", "vi-0006"}
    # Each table family owns a separate, org-scoped checkpoint.
    assert ("org-a", sn.SIR_CHECKPOINT_ID) in stored
    assert ("org-a", sn.VR_VULN_ITEM_CHECKPOINT_ID) in stored
    assert ("org-a", sn.VR_GROUP_CHECKPOINT_ID) in stored
    assert ("org-a", sn.VR_REMEDIATION_TASK_CHECKPOINT_ID) in stored


def test_ac1_failed_run_leaves_checkpoint_intact(monkeypatch):
    _offline(monkeypatch)
    cp_id = sn.VR_VULN_ITEM_CHECKPOINT_ID
    stored = {cp_id: Checkpoint.create(cp_id, "org-a", "2026-07-01 10:00:00")}
    result = ingest_with_checkpoint(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW),
        "org-a",
        process_batch=lambda b: (_ for _ in ()).throw(RuntimeError("graph write failed")),
        read_checkpoint=lambda o, c: stored.get(c),
        save_checkpoint=lambda cp: stored.__setitem__(cp.connector_id, cp),
    )
    assert not result.ok and not result.checkpoint_advanced
    assert stored[cp_id].value == "2026-07-01 10:00:00"


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — field scope: exploit/scanner/payload fields never cross the boundary
# ═════════════════════════════════════════════════════════════════════════════


def test_ac2_sir_signal_omits_forbidden_fields_and_seeded_values(monkeypatch):
    _offline(monkeypatch)
    records = _first_records(
        sn.ServiceNowSecurityIncidentChangeIngestor(org_id="org-a", clock=lambda: NOW))
    allowed = _sir_keys()
    for rec in records:
        assert not (set(rec) & sn.SIR_FORBIDDEN_FIELDS)
        assert set(rec) <= allowed, set(rec) - allowed
        blob = repr(rec)
        for marker in _SEED_MARKERS:
            assert marker not in blob


def test_ac2_vr_signal_omits_forbidden_fields_and_seeded_values(monkeypatch):
    _offline(monkeypatch)
    all_records = []
    for cls in (
        sn.ServiceNowVulnerableItemChangeIngestor,
        sn.ServiceNowVulnerabilityGroupChangeIngestor,
        sn.ServiceNowRemediationTaskChangeIngestor,
    ):
        all_records += _first_records(cls(org_id="org-a", clock=lambda: NOW))
    for rec in all_records:
        assert not (set(rec) & sn.VR_FORBIDDEN_FIELDS)
        blob = repr(rec)
        for marker in _SEED_MARKERS:
            assert marker not in blob
    items = _first_records(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW))
    allowed = _vi_keys()
    for item in items:
        assert set(item) <= allowed, set(item) - allowed


def test_ac2_live_projection_requests_only_workflow_fields(monkeypatch):
    calls = []

    class ReadOnlyClient:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            calls.append((table, dict(params)))
            return []

    monkeypatch.setattr(sn, "is_live", lambda: True)
    cases = [
        (sn.ServiceNowSecurityIncidentChangeIngestor, sn.SIR_FIELDS, sn.SIR_FORBIDDEN_FIELDS,
         sn.SIR_CHECKPOINT_ID),
        (sn.ServiceNowVulnerableItemChangeIngestor, sn.VR_VULN_ITEM_FIELDS, sn.VR_FORBIDDEN_FIELDS,
         sn.VR_VULN_ITEM_CHECKPOINT_ID),
    ]
    for cls, expected_fields, forbidden, cp_id in cases:
        calls.clear()
        ingestor = cls(org_id="org-a", client=ReadOnlyClient(), clock=lambda: NOW)
        list(ingestor.ingest_changes(
            "org-a", Checkpoint.create(cp_id, "org-a", "2026-07-01 10:00:00")))
        requested = set(calls[0][1]["sysparm_fields"].split(","))
        assert requested == set(expected_fields)
        assert not (requested & forbidden)


def test_ac2_seeded_scanner_content_never_reaches_logs(monkeypatch, caplog):
    _offline(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="discovery.ingest.servicenow"):
        _first_records(
            sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW))
    for marker in _SEED_MARKERS:
        assert marker not in caplog.text


# ═════════════════════════════════════════════════════════════════════════════
# AC3 — explicit item→CI join; item→CI→dependency provenance; fails closed
# ═════════════════════════════════════════════════════════════════════════════


def _entity(org_id, ci_id, ci_class="cmdb_ci_server") -> Entity:
    return Entity(
        org_id=org_id, entity_type="system", canonical_name=ci_id,
        display_name=ci_id.upper(), source_system="servicenow", source_record_id=ci_id,
        resolution_confidence=1.0, resolution_status="resolved",
        first_seen_run_id="run-cmdb", last_seen_run_id="run-cmdb",
        metadata={"ci_class": ci_class, "source_url": f"https://acme.service-now.com/{ci_id}"},
    )


def _vi(sys_id, cmdb_ci, org_id="org-a", state="Closed"):
    return {
        "sys_id": sys_id, "number": sys_id.upper(), "org_id": org_id,
        "vulnerability_class": "Missing Patch", "state": state,
        "state_history": [
            {"field": "state", "from_value": "Detected", "to_value": "Assigned",
             "changed_at": "2026-07-01 09:00:00"},
            {"field": "state", "from_value": "Assigned", "to_value": "Closed",
             "changed_at": "2026-07-02 09:00:00"},
        ],
        "cmdb_ci": cmdb_ci, "source_type": "servicenow_vulnerable_item",
        "source_url": f"https://acme.service-now.com/{sys_id}",
        "source_timestamp": "2026-07-02 10:00:00",
    }


def test_ac3_explicit_ci_join_carries_provenance_on_every_hop():
    org = "org-a"
    server = _entity(org, "ci-server-001", "cmdb_ci_server")
    database = _entity(org, "ci-db-001", "cmdb_ci_db_instance")
    relationship = {
        "sys_id": "rel-1", "relationship_type": "depends_on",
        "source_ci_id": "ci-server-001", "target_ci_id": "ci-db-001",
        "source_type": "servicenow_cmdb_rel_ci", "source_timestamp": "2026-07-01 08:00:00",
        "source_url": "https://acme.service-now.com/rel-1",
    }
    metrics = {"org_id": org, "vulnerable_items": [_vi("vi-1", "ci-server-001")]}

    counts = resolve_vulnerable_item_ci_references(
        org_id=org, vulnerable_item_metrics=metrics,
        cmdb_entities=[server, database], cmdb_relationships=[relationship],
    )
    assert counts == {"resolved": 1, "unresolved": 0}

    item = metrics["vulnerable_items"][0]
    assert item["ci_resolution"]["status"] == "resolved"
    assert item["resolved_ci"]["ci_class"] == "cmdb_ci_server"

    trace = item["ci_evidence_trace"]
    # Hop 1: vulnerable item → CI, observed, explicit reference.
    item_to_ci = trace["vulnerable_item_to_ci"]
    assert item_to_ci["origin"] == "observed"
    assert item_to_ci["relationship_type"] == "references"
    assert item_to_ci["vulnerable_item_sys_id"] == "vi-1"
    assert item_to_ci["ci_entity_id"] == str(server.id)
    # Hop 2: CI → dependency, observed, carries provenance.
    dep = trace["ci_dependencies"][0]
    assert dep["origin"] == "observed"
    assert dep["relationship_type"] == "depends_on"
    assert dep["from_ci_entity_id"] == str(server.id)
    assert dep["to_ci_entity_id"] == str(database.id)
    assert dep["source_record_id"] == "rel-1"


def test_ac3_unresolved_and_cross_org_fail_closed():
    org = "org-a"
    server = _entity(org, "ci-server-001")
    metrics = {
        "org_id": org,
        "vulnerable_items": [
            _vi("vi-unresolved", "ci-not-admitted"),   # references a CI not in scope
            _vi("vi-foreign", "ci-server-001", org_id="org-b"),  # cross-org record
        ],
    }
    counts = resolve_vulnerable_item_ci_references(
        org_id=org, vulnerable_item_metrics=metrics, cmdb_entities=[server])
    assert counts == {"resolved": 0, "unresolved": 2}

    by_id = {i["sys_id"]: i for i in metrics["vulnerable_items"]}
    assert by_id["vi-unresolved"]["ci_resolution"]["status"] == "unresolved"
    assert "resolved_ci" not in by_id["vi-unresolved"]
    assert by_id["vi-foreign"]["ci_resolution"]["reason"] == "organization_mismatch"
    assert "ci_evidence_trace" not in by_id["vi-foreign"]


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — deterministic remediation signatures with near-miss separation
# ═════════════════════════════════════════════════════════════════════════════


def test_ac4_remediation_signature_is_deterministic_and_normalized():
    a = compute_remediation_signature(
        vulnerability_class="  Missing  PATCH ", ci_class=" CMDB_CI_SERVER ",
        remediation_path=" Detected -> Assigned -> Closed ")
    b = compute_remediation_signature(
        vulnerability_class="missing patch", ci_class="cmdb_ci_server",
        remediation_path=["detected", "assigned", "closed"])
    assert a == b and a.startswith("1:") and len(a.split(":", 1)[1]) == 32


def test_ac4_near_miss_paths_and_classes_never_collapse():
    base = dict(vulnerability_class="missing patch", ci_class="cmdb_ci_server",
                remediation_path=["detected", "assigned", "in progress", "closed"])
    fixed = compute_remediation_signature(**base)
    # same vuln class, different remediation path → distinct signature.
    deferred = compute_remediation_signature(
        **{**base, "remediation_path": ["detected", "assigned", "deferred", "closed"]})
    # different CI class → distinct signature.
    other_ci = compute_remediation_signature(**{**base, "ci_class": "cmdb_ci_db_instance"})
    # different vuln class → distinct signature.
    other_class = compute_remediation_signature(**{**base, "vulnerability_class": "weak cipher"})
    assert len({fixed, deferred, other_ci, other_class}) == 4


# ═════════════════════════════════════════════════════════════════════════════
# AC5 — seeded IOCs/credentials in security notes never become retrievable
# ═════════════════════════════════════════════════════════════════════════════


def _security_incident(note, *, sys_id="sir-0001"):
    return {
        "sys_id": sys_id, "number": sys_id.upper(), "category": "Malicious code activity",
        "state": "Contain", "sys_updated_on": "2026-07-01 09:30:00",
        "source_url": f"https://acme.service-now.com/sn_si_incident.do?sys_id={sys_id}",
        "work_notes": note,
        "evidence": {
            "source_system": "servicenow", "source_artifact": sys_id, "origin": "observed",
            "source_artifact_type": "record_id",
            "source_url": f"https://acme.service-now.com/sn_si_incident.do?sys_id={sys_id}",
        },
    }


_IOC_NOTE = (
    "Beacon to 203.0.113.9 (defanged 10[.]0[.]0[.]5); C2 hxxps://evil.example.com/x. "
    "Sample sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855. "
    "Creds password=Sup3rSecretValue; key AKIAIOSFODNN7EXAMPLE. Followed runbook KB0010234."
)
_IOC_LEAKS = ("203.0.113.9", "10[.]0[.]0[.]5", "evil.example.com", "e3b0c44298fc",
              "Sup3rSecretValue", "AKIAIOSFODNN7EXAMPLE")


class _CapturingIngest:
    def __init__(self):
        self.calls = []

    def __call__(self, org_id, artifacts):
        self.calls.append((org_id, list(artifacts)))
        return {"org_id": org_id, "artifacts": len(artifacts)}


def test_ac5_production_sir_handoff_redacts_before_checkpoint(monkeypatch):
    _offline(monkeypatch)
    ingest = _CapturingIngest()
    stored, read_cp, save_cp = _checkpoint_store()
    monkeypatch.setattr(
        sn,
        "_read_security_incident_notes",
        lambda records, client=None: [
            _security_incident(_IOC_NOTE, sys_id=records[0]["sys_id"])
        ],
    )

    result = sn.ingest_sir_changes(
        org_id="org-a",
        run_id="run-notes",
        clock=lambda: FIRST_CLOCK,
        read_checkpoint=read_cp,
        save_checkpoint=save_cp,
        handoff_security_notes=True,
        security_note_ingest_fn=ingest,
        security_note_record_event_fn=lambda *args: None,
    )

    assert result["streams"]["sn_si_incident"]["checkpoint_advanced"] is True
    assert result["security_note_handoff"]["artifacts_handed_off"] == 1
    indexed = ingest.calls[0][1][0].content
    assert "[REDACTED:" in indexed
    assert all(secret not in indexed for secret in _IOC_LEAKS)


def test_ac5_failed_note_handoff_does_not_advance_checkpoint(monkeypatch):
    _offline(monkeypatch)
    stored, read_cp, save_cp = _checkpoint_store()
    monkeypatch.setattr(
        sn,
        "_read_security_incident_notes",
        lambda records, client=None: [
            _security_incident(_IOC_NOTE, sys_id=records[0]["sys_id"])
        ],
    )

    class _FailedIngest:
        artifacts_failed = 1

    result = sn.ingest_sir_changes(
        org_id="org-a",
        run_id="run-notes-failed",
        clock=lambda: FIRST_CLOCK,
        read_checkpoint=read_cp,
        save_checkpoint=save_cp,
        handoff_security_notes=True,
        security_note_ingest_fn=lambda *args: _FailedIngest(),
        security_note_record_event_fn=lambda *args: None,
    )

    stream = result["streams"]["sn_si_incident"]
    assert stream["checkpoint_advanced"] is False
    assert "security-note retrieval handoff failed" in stream["error"]
    assert ("org-a", sn.SIR_CHECKPOINT_ID) not in stored


def test_ac5_iocs_and_credentials_never_reach_retrievable_content():
    ingest = _CapturingIngest()
    ingest_security_notes(
        "org-a", [_security_incident(_IOC_NOTE)],
        ingest_fn=ingest, record_event_fn=lambda *a: None)
    content = ingest.calls[0][1][0].content
    for leak in _IOC_LEAKS:
        assert leak not in content, f"seeded value leaked into retrieval: {leak}"
    assert "[REDACTED:" in content
    assert "KB0010234" in content     # useful workflow prose survives


def test_ac5_note_reachable_only_via_access_controlled_evidence_pointer():
    artifact = build_security_note_artifact(_security_incident(_IOC_NOTE))[0]
    pointer = artifact.provenance["evidence_pointer"]
    assert pointer["source_system"] == "servicenow"
    assert pointer["source_artifact"] == "sir-0001"
    assert pointer["origin"] == "observed"
    # The pointer carries no note content; trace-back is via the source URL only.
    pointer_blob = json.dumps(pointer)
    for leak in _IOC_LEAKS:
        assert leak not in pointer_blob
    assert "service-now.com" in artifact.provenance["source_url"]


def test_ac5_base_scanner_unchanged_iocs_only_redacted_on_security_path():
    # The security extension must not aggressively IOC-redact ordinary IT content.
    base = scan_and_redact("deploy note: reach host 203.0.113.9")
    assert "203.0.113.9" in base.text and base.redacted is False


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — no signal or aggregate enumerates host×vulnerability pairs
# ═════════════════════════════════════════════════════════════════════════════


def test_ac6_vr_signal_carries_no_vulnerability_identity(monkeypatch):
    _offline(monkeypatch)
    items = _first_records(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW))
    for item in items:
        assert not (set(item) & _CVE_IDENTITY_FIELDS)


def test_ac6_workload_aggregate_has_no_host_or_vulnerability_axis(monkeypatch):
    _offline(monkeypatch)
    items = [
        {k: v for k, v in r.items() if k not in {"artifact_id", "change_kind"}}
        for r in _first_records(
            sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW))
    ]
    summary = sn.summarize_vulnerability_workload(items)
    assert set(summary) == {
        "total_items", "by_vulnerability_class", "by_severity_band", "by_assignment_group"}
    # No host id / CI reference / CVE appears anywhere in the rollup.
    blob = repr(summary)
    assert "ci-server-001" not in blob
    for marker in _SEED_MARKERS:
        assert marker not in blob


def test_ac6_volume_aggregates_are_workflow_level_only():
    report = run_secops_volume_validation(
        "org-a", vulnerable_items=2000, vulnerability_groups=100, remediation_tasks=200)
    assert report.host_vuln_enumeration_detected is False
    blob = json.dumps(report.workflow_aggregates)
    for forbidden in ("cve-", "CVE-", "vi-000", "cvss", "exploit", "scanner"):
        assert forbidden not in blob
    # Aggregation compresses volume into a bounded set of workflow patterns.
    assert report.aggregate_count < report.records_processed


# ═════════════════════════════════════════════════════════════════════════════
# AC7 — read-only, org-scoped; visible B7 budget reporting; safe checkpoint
# ═════════════════════════════════════════════════════════════════════════════


class _ReadOnlyOrgClient:
    instance_url = "https://tenant.service-now.com"

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def table_query(self, table, params, max_records):
        self.calls.append(table)
        return list(self._rows) if table == sn.VR_VULN_ITEM_TABLE else []

    def __getattr__(self, name):
        raise AssertionError(f"read-only client received a write call: {name!r}")


def _vi_row(sys_id, group):
    return {
        "sys_id": sys_id, "number": sys_id.upper(), "state": "Assigned",
        "vulnerability_class": "Missing Patch", "severity": "2 - High", "cmdb_ci": "ci-x",
        "assignment_group": group, "sys_created_on": "2026-07-01 09:00:00",
        "sys_updated_on": "2026-07-01 09:30:00",
    }


def test_ac7_two_org_runs_are_isolated_and_read_only(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: True)
    client_a = _ReadOnlyOrgClient([_vi_row("vi-a1", "Alpha")])
    client_b = _ReadOnlyOrgClient([_vi_row("vi-b1", "Bravo")])
    stored, read_cp, save_cp = _checkpoint_store()

    rec_a, rec_b = [], []
    ingest_with_checkpoint(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", client=client_a, clock=lambda: NOW),
        "org-a", process_batch=lambda b: rec_a.extend(b.records),
        read_checkpoint=read_cp, save_checkpoint=save_cp)
    ingest_with_checkpoint(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-b", client=client_b, clock=lambda: NOW),
        "org-b", process_batch=lambda b: rec_b.extend(b.records),
        read_checkpoint=read_cp, save_checkpoint=save_cp)

    assert {r["org_id"] for r in rec_a} == {"org-a"}
    assert {r["org_id"] for r in rec_b} == {"org-b"}
    assert {r["sys_id"] for r in rec_a} == {"vi-a1"}
    assert {r["sys_id"] for r in rec_b} == {"vi-b1"}
    assert ("org-a", sn.VR_VULN_ITEM_CHECKPOINT_ID) in stored
    assert ("org-b", sn.VR_VULN_ITEM_CHECKPOINT_ID) in stored
    # Read-only: only table reads issued.
    assert set(client_a.calls) <= {sn.VR_VULN_ITEM_TABLE, sn.SECOPS_AUDIT_TABLE}


def test_ac7_cross_org_checkpoint_rejected(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: True)
    ingestor = sn.ServiceNowVulnerableItemChangeIngestor(
        org_id="org-a", client=_ReadOnlyOrgClient([]), clock=lambda: NOW)
    foreign = Checkpoint.create(sn.VR_VULN_ITEM_CHECKPOINT_ID, "org-b", "2026-07-01 10:00:00")
    with pytest.raises(sn.ServiceNowIngestError, match="scope mismatch"):
        list(ingestor.ingest_changes("org-a", foreign))


def test_ac7_burst_within_budget_records_visible_measurements():
    report = run_secops_volume_validation(
        "org-a", vulnerable_items=3000, vulnerability_groups=100, remediation_tasks=200)
    assert report.budget == CALIBRATED_RUN_EVENT_BUDGET   # shared B7 budget, no independent limit
    assert report.records_deferred == 0
    assert report.records_processed == report.records_generated
    # Measurements are populated, not assumed.
    assert report.records_per_sec > 0 and report.peak_memory_mb > 0
    assert report.batch_count > 1
    assert report.envelope_pass, report.envelope_failures


def test_ac7_budget_breach_is_visible_and_never_silent_with_safe_checkpoint():
    report = run_secops_volume_validation(
        "org-a", vulnerable_items=5000, vulnerability_groups=0, remediation_tasks=0, budget=1000)
    br = report.budget_report
    assert br["breached"] is True and br["deferred"] == 4000
    assert br["reason"]                                     # human-readable, loud
    assert br["deferred_by_source"]["servicenow:sn_vul_vulnerable_item"] == 4000
    assert br["deferred_window"]["first"] and br["deferred_window"]["last"]
    # Nothing lost: seen == processed + deferred.
    assert report.records_seen == report.records_processed + report.records_deferred
    # Safe checkpoint preserved, strictly before the deferred window; resume is exact.
    checkpoint = report.safe_checkpoints[TABLE_VULNERABLE_ITEM]
    assert checkpoint and checkpoint < br["deferred_window"]["first"]
    assert report.resume_processed == 4000
    assert report.resume_duplicate_ids == 0 and report.resume_skipped_ids == 0


def test_ac7_volume_stream_reuses_the_b7_budget_report_shape():
    stream = SecOpsVolumeStream()
    assert stream.measurements("org-a").budget == CALIBRATED_RUN_EVENT_BUDGET
    assert set(stream.budget_report().to_dict()) == set(
        BudgetReport(budget=None, processed=0, deferred=0).to_dict())
    with pytest.raises(SecOpsOrgScopeError):
        stream.admit({"sys_id": "vi-1", "org_id": "org-b"},
                     table_family=TABLE_VULNERABLE_ITEM, org_id="org-a")


def test_ac7_budget_never_splits_equal_timestamp_checkpoint_boundary():
    stream = SecOpsVolumeStream(budget=2)
    rows = [
        {
            "sys_id": "sir-1",
            "org_id": "org-a",
            "source_timestamp": "2026-07-01 09:00:00",
            "category": "phishing",
        },
        {
            "sys_id": "sir-2",
            "org_id": "org-a",
            "source_timestamp": "2026-07-01 10:00:00",
            "category": "phishing",
        },
        {
            "sys_id": "sir-3",
            "org_id": "org-a",
            "source_timestamp": "2026-07-01 10:00:00",
            "category": "phishing",
        },
    ]
    admissions = stream.admit_records(
        rows, table_family=TABLE_SECURITY_INCIDENT, org_id="org-a"
    )
    report = stream.measurements("org-a")

    assert [a.disposition for a in admissions] == ["new", "deferred", "deferred"]
    assert report.safe_checkpoints[TABLE_SECURITY_INCIDENT] == "2026-07-01 09:00:00"
    assert report.budget_report["deferred_window"]["first"] == "2026-07-01 10:00:00"


def test_ac7_sir_and_vr_share_one_production_budget(monkeypatch):
    _offline(monkeypatch)
    stored, read_cp, save_cp = _checkpoint_store()
    stream = SecOpsVolumeStream(budget=2)

    sir = sn.ingest_sir_changes(
        org_id="org-a",
        run_id="run-shared-budget",
        clock=lambda: FIRST_CLOCK,
        read_checkpoint=read_cp,
        save_checkpoint=save_cp,
        volume_stream=stream,
    )
    vr = sn.ingest_vr_changes(
        org_id="org-a",
        run_id="run-shared-budget",
        clock=lambda: FIRST_CLOCK,
        read_checkpoint=read_cp,
        save_checkpoint=save_cp,
        volume_stream=stream,
    )

    assert len(sir["security_incidents"]) == 2
    assert vr["vulnerable_items"] == []
    assert vr["volume"]["budget_report"]["breached"] is True
    assert vr["volume"]["records_processed"] == 2
    assert vr["volume"]["records_deferred"] > 0
