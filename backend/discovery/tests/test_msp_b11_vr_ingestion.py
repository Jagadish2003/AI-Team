"""MSP-B11 T2: ServiceNow Vulnerability Response (VR) workflow ingestion.

Covers the T2 acceptance criteria at the ingestion layer:

  * AC1 — seeded vulnerable items, vulnerability groups, and remediation tasks
    ingest as workflow signal (lifecycle states, assignments, classifications,
    timestamps, transition history, resolvable provenance) and each stream is
    incremental on its own ``sys_updated_on`` checkpoint on the second run.
  * AC2 — scanner payloads, exploit descriptions, proof-of-concept content, raw
    findings, and specific CVE identity are seeded on the source records yet
    never appear in normalized records, workflow signals, or logs.
  * AC6 — no emitted signal or aggregate enumerates host×vulnerability pairs: the
    CVE identity is never ingested and the workload summary aggregates only by
    vulnerability class / severity band / assignment group.
  * AC7 (VR portion) — read-only and org-scoped: separate org-scoped checkpoints
    per stream, two-org isolation, cross-org checkpoints rejected.
"""
from __future__ import annotations

import logging
from dataclasses import fields
from datetime import datetime, timezone

import pytest

from discovery.ingest.base import Checkpoint
from discovery.ingest.change_runner import ingest_with_checkpoint
from discovery.ingest import servicenow as sn


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
FIRST_CLOCK = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)

_SEED_MARKERS = ("SEEDED OUT-OF-SCOPE", "CVE-2026-12345", "CVE-2026-55555", "CVSS:3.1")


def _offline(monkeypatch) -> None:
    monkeypatch.setattr(sn, "is_live", lambda: False)


def _item_keys() -> set:
    return {f.name for f in fields(sn.ServiceNowVulnerableItem)} | {"artifact_id", "change_kind"}


def _first_batch(ingestor, org_id="org-a", since=None):
    return list(ingestor.ingest_changes(org_id, since))[0]


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — vulnerable items: lifecycle, classification, deferral, history, provenance
# ─────────────────────────────────────────────────────────────────────────────


def test_first_run_ingests_vulnerable_item_workflow(monkeypatch):
    _offline(monkeypatch)
    ingestor = sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW)
    batch = _first_batch(ingestor)
    by_id = {r["sys_id"]: r for r in batch.records}

    assert set(by_id) == {f"vi-000{n}" for n in range(1, 7)}
    assert batch.next_checkpoint == "2026-07-10 12:00:00"

    item = by_id["vi-0001"]
    assert item["state"] == "In Progress"
    assert item["substate"] == "Remediation in progress"
    assert item["vulnerability_class"] == "Missing Patch"
    assert item["severity"] == "1 - Critical"
    assert item["risk_rating"] == "Critical"
    assert item["cmdb_ci"] == "ci-server-001"           # CI ref retained for the T3 join
    assert item["vulnerability_group"] == "vg-0001"
    assert item["assignment_group"] == "Vulnerability Management"
    assert item["first_found"] == "2026-06-25 03:00:00"
    assert item["opened_at"] == "2026-06-25 08:00:00"

    # Provenance.
    assert item["org_id"] == "org-a"
    assert item["source_timestamp"] == "2026-07-01 09:00:00"
    assert item["origin"] == "observed"
    assert item["source_type"] == sn.VR_VULN_ITEM_SOURCE_TYPE
    assert item["source_url"].startswith("https://example.service-now.com")
    assert "vi-0001" in item["source_url"]

    # Lifecycle history: state progression + a reassignment loop.
    assert [(t["from_value"], t["to_value"]) for t in item["state_history"]] == [
        ("Detected", "Assigned"),
        ("Assigned", "In Progress"),
    ]
    assert [(t["from_value"], t["to_value"]) for t in item["assignment_history"]] == [
        ("Vulnerability Management", "Server Team"),
        ("Server Team", "Vulnerability Management"),
    ]


def test_closure_deferral_and_exception_classifications(monkeypatch):
    _offline(monkeypatch)
    ingestor = sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW)
    by_id = {r["sys_id"]: r for r in _first_batch(ingestor).records}

    closed = by_id["vi-0004"]
    assert closed["state"] == "Closed"
    assert closed["close_code"] == "Fixed"
    assert closed["resolution_status"] == "Remediated"
    assert closed["resolved_at"] == "2026-06-30 18:00:00"
    assert closed["closed_at"] == "2026-06-30 20:00:00"

    deferred = by_id["vi-0003"]
    assert deferred["state"] == "Deferred"
    assert deferred["deferral_category"] == "Risk Accepted"
    assert deferred["justification_class"] == "Business Justification"

    exception = by_id["vi-0006"]
    assert exception["exception_category"] == "False Positive"
    assert exception["justification_class"] == "Analyst Reviewed"


def test_null_and_missing_fields_normalize_to_none(monkeypatch):
    _offline(monkeypatch)
    ingestor = sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW)
    by_id = {r["sys_id"]: r for r in _first_batch(ingestor).records}

    sparse = by_id["vi-0005"]
    assert sparse["state"] == "Under Review"
    assert sparse["vulnerability_class"] == "Weak Configuration"
    # Empty / missing fields become None, never "" or a crash.
    for empty_field in ("severity", "risk_rating", "cmdb_ci", "assignment_group",
                        "assigned_to", "closed_at", "deferral_category"):
        assert sparse[empty_field] is None
    assert tuple(sparse["state_history"]) == ()
    assert tuple(sparse["assignment_history"]) == ()


def test_first_run_ingests_groups_and_remediation_tasks(monkeypatch):
    _offline(monkeypatch)
    groups = list(
        sn.ServiceNowVulnerabilityGroupChangeIngestor(org_id="org-a", clock=lambda: NOW)
        .ingest_changes("org-a", None)
    )[0].records
    tasks = list(
        sn.ServiceNowRemediationTaskChangeIngestor(org_id="org-a", clock=lambda: NOW)
        .ingest_changes("org-a", None)
    )[0].records

    g = {r["sys_id"]: r for r in groups}
    assert set(g) == {"vg-0001", "vg-0002"}
    assert g["vg-0001"]["name"] == "Q3 Critical Patch Backlog"
    assert g["vg-0001"]["state"] == "In Progress"
    assert g["vg-0001"]["remediation_status"] == "Active"
    assert g["vg-0001"]["assignment_group"] == "Vulnerability Management"
    assert g["vg-0001"]["source_type"] == sn.VR_GROUP_SOURCE_TYPE
    assert [(t["from_value"], t["to_value"]) for t in g["vg-0001"]["state_history"]] == [
        ("New", "In Progress"),
    ]

    t = {r["sys_id"]: r for r in tasks}
    assert set(t) == {"rt-0001", "rt-0002"}
    assert t["rt-0001"]["state"] == "Open"
    assert t["rt-0001"]["assignment_group"] == "Server Team"
    assert t["rt-0001"]["vulnerability_group"] == "vg-0001"
    assert t["rt-0001"]["due_date"] == "2026-07-15 00:00:00"
    assert t["rt-0002"]["close_code"] == "Completed"
    assert t["rt-0001"]["source_type"] == sn.VR_REMEDIATION_TASK_SOURCE_TYPE


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — field scope: scanner/exploit/finding/CVE content never crosses over
# ─────────────────────────────────────────────────────────────────────────────


def test_out_of_scope_fields_absent_from_all_vr_records(monkeypatch):
    _offline(monkeypatch)
    all_records = []
    for cls in (
        sn.ServiceNowVulnerableItemChangeIngestor,
        sn.ServiceNowVulnerabilityGroupChangeIngestor,
        sn.ServiceNowRemediationTaskChangeIngestor,
    ):
        all_records += list(cls(org_id="org-a", clock=lambda: NOW).ingest_changes("org-a", None))[0].records

    allowed = _item_keys()
    for record in all_records:
        leaked = set(record) & sn.VR_FORBIDDEN_FIELDS
        assert not leaked, f"scope leak: {leaked}"
        blob = repr(record)
        for marker in _SEED_MARKERS:
            assert marker not in blob, f"seeded value leaked: {marker}"
    # The item stream never emits a key outside its declared workflow scope.
    items = list(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW)
        .ingest_changes("org-a", None)
    )[0].records
    for item in items:
        assert set(item) <= allowed, set(item) - allowed


def test_seeded_scanner_content_never_appears_in_logs(monkeypatch, caplog):
    _offline(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="discovery.ingest.servicenow"):
        list(
            sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW)
            .ingest_changes("org-a", None)
        )
    for marker in _SEED_MARKERS:
        assert marker not in caplog.text


def test_live_fetch_projects_only_workflow_fields(monkeypatch):
    calls = []

    class ReadOnlyClient:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            calls.append((table, dict(params), max_records))
            return []

    monkeypatch.setattr(sn, "is_live", lambda: True)
    cases = [
        (sn.ServiceNowVulnerableItemChangeIngestor, sn.VR_VULN_ITEM_TABLE, sn.VR_VULN_ITEM_FIELDS),
        (sn.ServiceNowVulnerabilityGroupChangeIngestor, sn.VR_GROUP_TABLE, sn.VR_GROUP_FIELDS),
        (sn.ServiceNowRemediationTaskChangeIngestor, sn.VR_REMEDIATION_TASK_TABLE, sn.VR_REMEDIATION_TASK_FIELDS),
    ]
    for cls, table, expected_fields in cases:
        calls.clear()
        ingestor = cls(org_id="org-a", client=ReadOnlyClient(), clock=lambda: NOW)
        list(ingestor.ingest_changes("org-a", Checkpoint.create(cls.connector_id, "org-a", "2026-07-01 10:00:00")))
        first = calls[0]
        assert first[0] == table
        assert first[1]["sysparm_fields"] == ",".join(expected_fields)
        assert first[2] == sn.VR_RECORD_CAP
        assert first[1]["sysparm_query"] == (
            "sys_updated_on>2026-07-01 10:00:00^"
            "sys_updated_on<=2026-07-10 12:00:00^"
            "ORDERBYsys_updated_on^ORDERBYsys_id"
        )
        # No forbidden field is ever named in any VR projection.
        assert not (set(first[1]["sysparm_fields"].split(",")) & sn.VR_FORBIDDEN_FIELDS)


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — no host×vulnerability enumeration
# ─────────────────────────────────────────────────────────────────────────────


def test_no_host_by_vulnerability_pair_in_signal_or_aggregate(monkeypatch):
    _offline(monkeypatch)
    items = [
        {k: v for k, v in r.items() if k not in {"artifact_id", "change_kind"}}
        for r in list(
            sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", clock=lambda: NOW)
            .ingest_changes("org-a", None)
        )[0].records
    ]

    # No item carries a specific vulnerability/CVE identity — so a CI reference
    # can never be paired with a CVE to enumerate exposure.
    identity_fields = {"vulnerability", "vulnerability_id", "cve", "cve_id", "cvss_vector"}
    for item in items:
        assert not (set(item) & identity_fields)

    summary = sn.summarize_vulnerability_workload(items)
    # The aggregate keys only on classification / band / group — no host axis,
    # no vulnerability-identity axis, so no (host, vulnerability) tuple exists.
    assert set(summary) == {
        "total_items", "by_vulnerability_class", "by_severity_band", "by_assignment_group",
    }
    assert summary["total_items"] == len(items)
    assert summary["by_vulnerability_class"]["Missing Patch"] == 2
    assert "ci-server-001" not in repr(summary)  # no host axis anywhere in the rollup


# ─────────────────────────────────────────────────────────────────────────────
# AC1 incremental + AC7 separate org-scoped checkpoints (orchestration)
# ─────────────────────────────────────────────────────────────────────────────


def test_streams_are_incremental_with_separate_checkpoints(monkeypatch):
    _offline(monkeypatch)
    stored: dict[tuple[str, str], Checkpoint] = {}

    def read_cp(org_id, connector_id):
        return stored.get((org_id, connector_id))

    def save_cp(cp):
        stored[(cp.org_id, cp.connector_id)] = cp

    run1 = sn.ingest_vr_changes(
        org_id="org-a", run_id="r1", clock=lambda: FIRST_CLOCK,
        read_checkpoint=read_cp, save_checkpoint=save_cp,
    )
    assert {i["sys_id"] for i in run1["vulnerable_items"]} == {
        "vi-0001", "vi-0002", "vi-0003", "vi-0004"
    }
    assert {g["sys_id"] for g in run1["vulnerability_groups"]} == {"vg-0001"}
    assert {t["sys_id"] for t in run1["remediation_tasks"]} == {"rt-0001"}

    # Three separate, org-scoped checkpoints — one per VR table stream.
    assert set(stored) == {
        ("org-a", sn.VR_VULN_ITEM_CHECKPOINT_ID),
        ("org-a", sn.VR_GROUP_CHECKPOINT_ID),
        ("org-a", sn.VR_REMEDIATION_TASK_CHECKPOINT_ID),
    }
    for stream in run1["streams"].values():
        assert stream["error"] is None and stream["checkpoint_advanced"] is True

    run2 = sn.ingest_vr_changes(
        org_id="org-a", run_id="r2", clock=lambda: NOW,
        read_checkpoint=read_cp, save_checkpoint=save_cp,
    )
    # Second run reads ONLY records updated after each stream's stored cursor.
    assert {i["sys_id"] for i in run2["vulnerable_items"]} == {"vi-0005", "vi-0006"}
    assert {g["sys_id"] for g in run2["vulnerability_groups"]} == {"vg-0002"}
    assert {t["sys_id"] for t in run2["remediation_tasks"]} == {"rt-0002"}


def test_failed_run_leaves_checkpoint_intact(monkeypatch):
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


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — read-only and org-scoped (two-org)
# ─────────────────────────────────────────────────────────────────────────────


class _ReadOnlyOrgClient:
    instance_url = "https://tenant.service-now.com"

    def __init__(self, item_rows):
        self._item_rows = item_rows
        self.calls = []

    def table_query(self, table, params, max_records):
        self.calls.append(table)
        if table == sn.VR_VULN_ITEM_TABLE:
            return list(self._item_rows)
        return []

    def __getattr__(self, name):
        raise AssertionError(f"read-only client received a write call: {name!r}")


def _vi_row(sys_id, group):
    return {
        "sys_id": sys_id,
        "number": sys_id.upper(),
        "state": "Assigned",
        "vulnerability_class": "Missing Patch",
        "severity": "2 - High",
        "cmdb_ci": "ci-x",
        "assignment_group": group,
        "sys_created_on": "2026-07-01 09:00:00",
        "sys_updated_on": "2026-07-01 09:30:00",
    }


def test_two_org_runs_are_isolated_and_read_only(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: True)
    client_a = _ReadOnlyOrgClient([_vi_row("vi-a1", "Alpha Vuln")])
    client_b = _ReadOnlyOrgClient([_vi_row("vi-b1", "Bravo Vuln")])
    stored: dict[tuple[str, str], Checkpoint] = {}

    def read_cp(org_id, connector_id):
        return stored.get((org_id, connector_id))

    def save_cp(cp):
        stored[(cp.org_id, cp.connector_id)] = cp

    rec_a, rec_b = [], []
    ingest_with_checkpoint(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-a", client=client_a, clock=lambda: NOW),
        "org-a", process_batch=lambda b: rec_a.extend(b.records),
        read_checkpoint=read_cp, save_checkpoint=save_cp,
    )
    ingest_with_checkpoint(
        sn.ServiceNowVulnerableItemChangeIngestor(org_id="org-b", client=client_b, clock=lambda: NOW),
        "org-b", process_batch=lambda b: rec_b.extend(b.records),
        read_checkpoint=read_cp, save_checkpoint=save_cp,
    )

    assert {r["sys_id"] for r in rec_a} == {"vi-a1"}
    assert {r["sys_id"] for r in rec_b} == {"vi-b1"}
    assert {r["org_id"] for r in rec_a} == {"org-a"}
    assert {r["org_id"] for r in rec_b} == {"org-b"}
    assert ("org-a", sn.VR_VULN_ITEM_CHECKPOINT_ID) in stored
    assert ("org-b", sn.VR_VULN_ITEM_CHECKPOINT_ID) in stored
    # Read-only: only table reads were issued.
    assert set(client_a.calls) <= {sn.VR_VULN_ITEM_TABLE, sn.SECOPS_AUDIT_TABLE}


def test_all_vr_streams_reject_cross_org_checkpoints(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: True)
    classes = [
        sn.ServiceNowVulnerableItemChangeIngestor,
        sn.ServiceNowVulnerabilityGroupChangeIngestor,
        sn.ServiceNowRemediationTaskChangeIngestor,
    ]
    # Distinct checkpoint ids so each stream advances independently.
    assert len({c.connector_id for c in classes}) == 3
    for cls in classes:
        ingestor = cls(org_id="org-a", client=_ReadOnlyOrgClient([]), clock=lambda: NOW)
        foreign = Checkpoint.create(cls.connector_id, "org-b", "2026-07-01 10:00:00")
        with pytest.raises(sn.ServiceNowIngestError, match="scope mismatch"):
            list(ingestor.ingest_changes("org-a", foreign))
        with pytest.raises(sn.ServiceNowIngestError, match="organization mismatch"):
            list(ingestor.ingest_changes("org-b", None))
