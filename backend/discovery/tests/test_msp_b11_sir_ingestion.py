"""MSP-B11 T1: ServiceNow Security Incident Response (SIR) workflow ingestion.

Proves the T1 acceptance criteria at the ingestion layer:

  * AC1 — seeded SIR records ingest as workflow signal (states, assignments,
    classifications, timestamps, transition history, resolvable provenance) and
    the second run reads only updated records (incremental on ``sys_updated_on``).
  * AC2 — fields outside the configured workflow scope (exploit detail, scanner
    payload, notes, IOCs, credentials) are absent from the emitted signal, even
    though they are seeded on the source records ("workload, not weakness").
  * AC7 — read-only and org-scoped: a two-org run keeps each org's signal and
    checkpoint isolated, cross-org checkpoints are rejected, and the connector
    only ever issues read queries.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from discovery.ingest.base import Checkpoint
from discovery.ingest.change_runner import ingest_with_checkpoint
from discovery.ingest import servicenow as sn


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

# Every field a SIR workflow signal is allowed to carry (the dataclass fields
# plus the runner's delta envelope). Anything else is a scope leak.
_ALLOWED_SIGNAL_KEYS = {
    "sys_id", "number", "state", "category", "subcategory", "severity",
    "priority", "assigned_to", "assignment_group", "opened_at", "created_at",
    "resolved_at", "closed_at", "close_code", "resolution_code",
    "state_history", "assignment_history", "org_id", "source_timestamp",
    "source_url", "source_type", "origin", "artifact_id", "change_kind",
}


def _checkpoint(value: str, org_id: str = "org-a") -> Checkpoint:
    return Checkpoint.create(sn.SIR_CHECKPOINT_ID, org_id, value)


def _offline(monkeypatch) -> None:
    monkeypatch.setattr(sn, "is_live", lambda: False)


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — workflow data, history, and provenance on the first run
# ─────────────────────────────────────────────────────────────────────────────


def test_first_run_ingests_workflow_fields_history_and_provenance(monkeypatch):
    _offline(monkeypatch)
    ingestor = sn.ServiceNowSecurityIncidentChangeIngestor(
        org_id="org-a", clock=lambda: NOW
    )

    batch = list(ingestor.ingest_changes("org-a", None))[0]
    by_id = {rec["sys_id"]: rec for rec in batch.records}

    # All seeded records within the watermark window are ingested.
    assert set(by_id) == {"sir-0001", "sir-0002", "sir-0003", "sir-0004"}
    assert batch.next_checkpoint == "2026-07-10 12:00:00"

    contained = by_id["sir-0001"]
    # Workflow scalars + classifications.
    assert contained["state"] == "Contain"
    assert contained["category"] == "Malicious code activity"
    assert contained["subcategory"] == "Ransomware"
    assert contained["severity"] == "1 - High"
    assert contained["priority"] == "1 - Critical"
    assert contained["assignment_group"] == "SecOps Triage"
    assert contained["assigned_to"] == "Dana Cruz"
    assert contained["opened_at"] == "2026-06-30 08:00:00"

    # Close classification captured on a closed record.
    closed = by_id["sir-0003"]
    assert closed["state"] == "Closed"
    assert closed["close_code"] == "Resolved by mitigation"
    assert closed["resolution_code"] == "True positive - remediated"
    assert closed["resolved_at"] == "2026-06-30 16:20:00"
    assert closed["closed_at"] == "2026-06-30 17:00:00"

    # State + assignment history captures a triage loop / handoff (Triage → Tier 2
    # → Triage) so downstream detectors can spot repeated handoffs and loops.
    state_hist = contained["state_history"]
    assert [(t["from_value"], t["to_value"]) for t in state_hist] == [
        ("Draft", "Analysis"),
        ("Analysis", "Contain"),
    ]
    assign_hist = contained["assignment_history"]
    assert [(t["from_value"], t["to_value"]) for t in assign_hist] == [
        ("SecOps Triage", "SecOps Tier 2"),
        ("SecOps Tier 2", "SecOps Triage"),
    ]

    # Provenance: org, source record, source timestamp, observed origin.
    assert contained["org_id"] == "org-a"
    assert contained["number"] == "SIR0010001"
    assert contained["source_timestamp"] == "2026-07-01 09:30:00"
    assert contained["origin"] == "observed"
    assert contained["source_type"] == sn.SIR_SOURCE_TYPE
    assert contained["source_url"].startswith("https://example.service-now.com")
    assert "sir-0001" in contained["source_url"]


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — field scope: out-of-scope fields never cross the boundary
# ─────────────────────────────────────────────────────────────────────────────


def test_out_of_scope_fields_are_absent_from_the_signal(monkeypatch):
    _offline(monkeypatch)
    ingestor = sn.ServiceNowSecurityIncidentChangeIngestor(
        org_id="org-a", clock=lambda: NOW
    )
    batch = list(ingestor.ingest_changes("org-a", None))[0]

    for record in batch.records:
        # No forbidden key survives to the emitted signal...
        leaked = set(record) & sn.SIR_FORBIDDEN_FIELDS
        assert not leaked, f"scope leak: {leaked}"
        # ...and the record carries only allow-listed workflow keys.
        assert set(record) <= _ALLOWED_SIGNAL_KEYS, set(record) - _ALLOWED_SIGNAL_KEYS
        # ...and no seeded sensitive value slipped through under any key.
        serialized = repr(record)
        assert "SEEDED OUT-OF-SCOPE" not in serialized
        assert "hunter2" not in serialized


def test_live_fetch_projects_only_workflow_fields(monkeypatch):
    """The Table API request itself asks for the bounded field scope only."""
    calls = []

    class ReadOnlyClient:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            calls.append((table, dict(params), max_records))
            return []

    monkeypatch.setattr(sn, "is_live", lambda: True)
    ingestor = sn.ServiceNowSecurityIncidentChangeIngestor(
        org_id="org-a", client=ReadOnlyClient(), clock=lambda: NOW
    )
    list(ingestor.ingest_changes("org-a", _checkpoint("2026-07-01 10:00:00")))

    table, params, cap = calls[0]
    assert table == sn.SIR_TABLE
    assert params["sysparm_fields"] == ",".join(sn.SIR_FIELDS)
    assert cap == sn.SIR_RECORD_CAP
    assert params["sysparm_query"] == (
        "sys_updated_on>2026-07-01 10:00:00^"
        "sys_updated_on<=2026-07-10 12:00:00^"
        "ORDERBYsys_updated_on^ORDERBYsys_id"
    )
    # No sensitive-content field is ever named in the projection.
    requested = set(params["sysparm_fields"].split(","))
    assert not (requested & sn.SIR_FORBIDDEN_FIELDS)


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — incremental: the second run reads only updated records
# ─────────────────────────────────────────────────────────────────────────────


def test_second_run_reads_only_updated_records(monkeypatch):
    _offline(monkeypatch)
    stored: dict[str, Checkpoint] = {}

    def read_checkpoint(org_id, connector_id):
        assert org_id == "org-a"
        return stored.get(connector_id)

    def save_checkpoint(cp):
        stored[cp.connector_id] = cp

    # First run: watermark clock at 2026-07-01 10:00:00 admits records updated up
    # to that instant (sir-0001, sir-0003).
    first_clock = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    first_records: list[dict] = []
    r1 = ingest_with_checkpoint(
        sn.ServiceNowSecurityIncidentChangeIngestor(
            org_id="org-a", clock=lambda: first_clock
        ),
        "org-a",
        process_batch=lambda b: first_records.extend(b.records),
        read_checkpoint=read_checkpoint,
        save_checkpoint=save_checkpoint,
    )
    assert r1.ok and r1.checkpoint_advanced
    assert {r["sys_id"] for r in first_records} == {"sir-0001", "sir-0003"}
    assert stored[sn.SIR_CHECKPOINT_ID].value == "2026-07-01 10:00:00"

    # Second run: later watermark. Only records updated AFTER the stored cursor
    # (sir-0002 at 11:00, sir-0004 on 07-03) are read — never the already-seen ones.
    second_records: list[dict] = []
    r2 = ingest_with_checkpoint(
        sn.ServiceNowSecurityIncidentChangeIngestor(
            org_id="org-a", clock=lambda: NOW
        ),
        "org-a",
        process_batch=lambda b: second_records.extend(b.records),
        read_checkpoint=read_checkpoint,
        save_checkpoint=save_checkpoint,
    )
    assert r2.ok and r2.checkpoint_advanced
    assert {r["sys_id"] for r in second_records} == {"sir-0002", "sir-0004"}
    assert stored[sn.SIR_CHECKPOINT_ID].value == "2026-07-10 12:00:00"


def test_failed_run_leaves_checkpoint_intact(monkeypatch):
    _offline(monkeypatch)
    stored = {sn.SIR_CHECKPOINT_ID: _checkpoint("2026-07-01 10:00:00")}

    result = ingest_with_checkpoint(
        sn.ServiceNowSecurityIncidentChangeIngestor(org_id="org-a", clock=lambda: NOW),
        "org-a",
        process_batch=lambda b: (_ for _ in ()).throw(RuntimeError("graph write failed")),
        read_checkpoint=lambda o, c: stored.get(c),
        save_checkpoint=lambda cp: stored.__setitem__(cp.connector_id, cp),
    )
    assert not result.ok and not result.checkpoint_advanced
    # A failed/partial run must leave the last valid checkpoint untouched.
    assert stored[sn.SIR_CHECKPOINT_ID].value == "2026-07-01 10:00:00"


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — read-only and org-scoped (two-org)
# ─────────────────────────────────────────────────────────────────────────────


class _ReadOnlyOrgClient:
    """Fake ServiceNow client that serves per-org rows and rejects any write."""

    instance_url = "https://tenant.service-now.com"

    def __init__(self, sir_rows):
        self._sir_rows = sir_rows
        self.calls = []

    def table_query(self, table, params, max_records):
        self.calls.append(table)
        if table == sn.SIR_TABLE:
            return list(self._sir_rows)
        return []  # audit history — empty for this test

    def __getattr__(self, name):  # any non-read method is a contract violation
        raise AssertionError(f"read-only client received a write call: {name!r}")


def _row(sys_id, number, group):
    return {
        "sys_id": sys_id,
        "number": number,
        "state": "Analysis",
        "category": "Unauthorized access",
        "severity": "2 - Medium",
        "assignment_group": group,
        "sys_created_on": "2026-07-01 09:00:00",
        "sys_updated_on": "2026-07-01 09:30:00",
    }


def test_two_org_runs_are_isolated_and_read_only(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: True)
    client_a = _ReadOnlyOrgClient([_row("sir-a1", "SIRA001", "Alpha SecOps")])
    client_b = _ReadOnlyOrgClient([_row("sir-b1", "SIRB001", "Bravo SecOps")])

    stored: dict[tuple[str, str], Checkpoint] = {}

    def read_checkpoint(org_id, connector_id):
        return stored.get((org_id, connector_id))

    def save_checkpoint(cp):
        stored[(cp.org_id, cp.connector_id)] = cp

    rec_a, rec_b = [], []
    ra = ingest_with_checkpoint(
        sn.ServiceNowSecurityIncidentChangeIngestor(
            org_id="org-a", client=client_a, clock=lambda: NOW
        ),
        "org-a",
        process_batch=lambda b: rec_a.extend(b.records),
        read_checkpoint=read_checkpoint, save_checkpoint=save_checkpoint,
    )
    rb = ingest_with_checkpoint(
        sn.ServiceNowSecurityIncidentChangeIngestor(
            org_id="org-b", client=client_b, clock=lambda: NOW
        ),
        "org-b",
        process_batch=lambda b: rec_b.extend(b.records),
        read_checkpoint=read_checkpoint, save_checkpoint=save_checkpoint,
    )

    assert ra.ok and rb.ok
    # No cross-org bleed in either the data or its stamped provenance.
    assert {r["sys_id"] for r in rec_a} == {"sir-a1"}
    assert {r["sys_id"] for r in rec_b} == {"sir-b1"}
    assert {r["org_id"] for r in rec_a} == {"org-a"}
    assert {r["org_id"] for r in rec_b} == {"org-b"}
    # Checkpoints are keyed per org — distinct rows, no collision.
    assert set(stored) == {
        ("org-a", sn.SIR_CHECKPOINT_ID),
        ("org-b", sn.SIR_CHECKPOINT_ID),
    }
    # Read-only: every query issued was a table read.
    assert set(client_a.calls) <= {sn.SIR_TABLE, sn.SIR_AUDIT_TABLE}
    assert set(client_b.calls) <= {sn.SIR_TABLE, sn.SIR_AUDIT_TABLE}


def test_cross_org_checkpoint_is_rejected(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: True)
    ingestor = sn.ServiceNowSecurityIncidentChangeIngestor(
        org_id="org-a", client=_ReadOnlyOrgClient([]), clock=lambda: NOW
    )
    foreign = _checkpoint("2026-07-01 10:00:00", org_id="org-b")
    with pytest.raises(sn.ServiceNowIngestError, match="scope mismatch"):
        list(ingestor.ingest_changes("org-a", foreign))
    with pytest.raises(sn.ServiceNowIngestError, match="organization mismatch"):
        list(ingestor.ingest_changes("org-b", None))


def test_malicious_checkpoint_rejected_before_any_request(monkeypatch):
    class Client:
        instance_url = "https://acme.service-now.com"

        def table_query(self, *a, **k):
            raise AssertionError("invalid cursor must not reach ServiceNow")

    monkeypatch.setattr(sn, "is_live", lambda: True)
    ingestor = sn.ServiceNowSecurityIncidentChangeIngestor(
        org_id="org-a", client=Client()
    )
    malicious = _checkpoint("2026-07-01 10:00:00^ORnumberLIKEsecret")
    with pytest.raises(sn.ServiceNowIngestError, match="invalid.*checkpoint"):
        list(ingestor.ingest_changes("org-a", malicious))


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration entry point (on the shared change-ingestion rails)
# ─────────────────────────────────────────────────────────────────────────────


def test_ingest_sir_changes_returns_bounded_workflow_signal(monkeypatch):
    _offline(monkeypatch)
    saved: list[Checkpoint] = []

    payload = sn.ingest_sir_changes(
        org_id="org-a",
        run_id="run-123",
        clock=lambda: NOW,
        read_checkpoint=lambda org_id, connector_id: None,
        save_checkpoint=saved.append,
    )

    assert payload["org_id"] == "org-a"
    assert payload["run_id"] == "run-123"
    assert {i["sys_id"] for i in payload["security_incidents"]} == {
        "sir-0001", "sir-0002", "sir-0003", "sir-0004"
    }
    stream = payload["streams"]["sn_si_incident"]
    assert stream["connector_id"] == sn.SIR_CHECKPOINT_ID
    assert stream["error"] is None
    assert stream["checkpoint_advanced"] is True
    # No scope leak through the orchestration layer either.
    for incident in payload["security_incidents"]:
        assert not (set(incident) & sn.SIR_FORBIDDEN_FIELDS)
