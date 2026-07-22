"""R-1.9.1-L2 / T4 (AT-696) — tamper-evidence: event-count + hash chain (AC4).

AC4: removing a billing event from the store before generation renders the
report detectably inconsistent (hash-chain/count mismatch).

The mechanism: each billing event is stamped at emission with a per-org monotonic
``seq``. The usage report covers a contiguous seq block, so a deleted (mid-stream)
event leaves a GAP — the report's sequenced-event count no longer matches its seq
range — and the hash chain over the covered events re-verifies that.

Most tests are DB-free (the chain build/verify is pure; the report builder is
driven with monkeypatched telemetry). The per-org seq counter (``next_seq``) and a
true end-to-end delete-and-regenerate use the contract Postgres → CI.
"""
from __future__ import annotations

import json

import pytest

from app import billing_chain as bc
from app import usage_report as ur


# ---------------------------------------------------------------------------
# billing_chain — build + verify (pure)
# ---------------------------------------------------------------------------
def _run(seq, run_id):
    return {"seq": seq, "event_type": ur.BILLING_RUN_COMPLETED, "core": {"run_id": run_id, "ai_mode": "hosted"}}


def test_contiguous_events_are_consistent():
    te = bc.build_tamper_evidence([_run(1, "r1"), _run(2, "r2"), _run(3, "r3")], total_event_count=3)
    assert te["consistent"] is True
    assert te["seq_min"] == 1 and te["seq_max"] == 3
    assert te["expected_count"] == 3 and te["sequenced_count"] == 3
    assert len(te["chain"]) == 3 and te["chain_root"]
    assert bc.verify_tamper_evidence(te)["consistent"] is True


def test_deleted_middle_event_is_a_detectable_gap():
    """AC4: a deleted mid-stream event (seq 2 gone → 1,3) is a seq gap the report
    surfaces as inconsistent."""
    te = bc.build_tamper_evidence([_run(1, "r1"), _run(3, "r3")], total_event_count=2)
    assert te["consistent"] is False
    assert te["expected_count"] == 3 and te["sequenced_count"] == 2
    v = bc.verify_tamper_evidence(te)
    assert v["consistent"] is False
    assert v["gap_detected"] is True


def test_altered_chain_fails_reverification():
    te = bc.build_tamper_evidence([_run(1, "r1"), _run(2, "r2")], total_event_count=2)
    te["chain"][1]["entry_hash"] = "deadbeef"  # tamper with a chain entry
    v = bc.verify_tamper_evidence(te)
    assert v["chain_root_matches"] is False
    assert v["consistent"] is False


def test_unsequenced_events_are_flagged():
    te = bc.build_tamper_evidence(
        [_run(1, "r1"), {"seq": None, "event_type": ur.BILLING_RUN_COMPLETED, "core": {"run_id": "rX"}}],
        total_event_count=2,
    )
    assert te["sequenced_count"] == 1 and te["unsequenced_count"] == 1
    assert te["consistent"] is False
    assert bc.verify_tamper_evidence(te)["consistent"] is False


def test_empty_report_is_trivially_consistent():
    te = bc.build_tamper_evidence([], total_event_count=0)
    assert te["consistent"] is True
    assert te["chain"] == [] and te["chain_root"] == ""
    assert bc.verify_tamper_evidence(te)["consistent"] is True


def test_entry_hash_binds_content():
    """Two events with the same seq but different content hash differently, so the
    chain binds what the report shows (content tampering changes the chain)."""
    a = bc.entry_hash(1, ur.BILLING_RUN_COMPLETED, {"run_id": "r1", "ai_mode": "hosted"})
    b = bc.entry_hash(1, ur.BILLING_RUN_COMPLETED, {"run_id": "r1", "ai_mode": "customer_tenant"})
    assert a != b


# ---------------------------------------------------------------------------
# usage_report integration — the report carries tamper_evidence, and a deleted
# event shows up as an inconsistent report (DB-free via monkeypatched telemetry).
# ---------------------------------------------------------------------------
class _Ev:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload)


def _range_returning(runs):
    def _range(org_id, event_type, from_dt, to_dt, limit=10000):
        if event_type == ur.BILLING_RUN_COMPLETED:
            return [_Ev(p) for p in runs]
        return []
    return _range


def _run_payload(seq, run_id, mode="hosted"):
    return {
        "run_id": run_id, "ai_mode": mode, "connected_system_count": 3,
        "pack_ids": ["service_cloud"], "completed_at": f"2026-07-{seq:02d}T10:00:00+00:00",
        "seq": seq,
    }


def test_report_includes_consistent_tamper_evidence(monkeypatch):
    intact = [_run_payload(1, "r1"), _run_payload(2, "r2"), _run_payload(3, "r3")]
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _range_returning(intact))
    body = ur.build_usage_report_body(
        "org-A", "2026-07-01", "2026-07-31", kid="cf-2026-1", license_org_id="org-A",
        generated_at="2026-08-01T00:00:00+00:00",
    )
    te = body["tamper_evidence"]
    assert te["consistent"] is True
    assert te["event_count"] == 3
    assert bc.verify_tamper_evidence(te)["consistent"] is True
    # per-run entries carry the seq for traceability.
    assert sorted(r["seq"] for r in body["runs"]["per_run"]) == [1, 2, 3]


def test_report_detects_a_deleted_event(monkeypatch):
    """AC4 end-to-end (logic): a report generated after an event was deleted from
    the store (seq 2 missing → the store now yields seq 1, 3) is detectably
    inconsistent."""
    after_delete = [_run_payload(1, "r1"), _run_payload(3, "r3")]  # seq 2 deleted
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _range_returning(after_delete))
    body = ur.build_usage_report_body(
        "org-A", "2026-07-01", "2026-07-31", kid="cf-2026-1", license_org_id="org-A",
        generated_at="2026-08-01T00:00:00+00:00",
    )
    te = body["tamper_evidence"]
    assert te["consistent"] is False
    assert te["sequenced_count"] == 2 and te["expected_count"] == 3
    assert bc.verify_tamper_evidence(te)["gap_detected"] is True


def test_signature_covers_tamper_evidence(monkeypatch):
    """The T3 signature covers the whole body including tamper_evidence, so the
    chain/counts cannot be altered after generation without breaking the signature."""
    monkeypatch.setattr(
        "app.usage_report.get_current_license_status",
        lambda **k: {"status": "valid", "payload": {"report_key": "rk-abc", "kid": "cf-2026-1", "org_id": "org-A"}},
    )
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _range_returning([_run_payload(1, "r1"), _run_payload(2, "r2")]),
    )
    env = ur.generate_signed_report("org-A", "2026-07-01", "2026-07-31")
    body = env["report"]
    assert body["tamper_evidence"]["consistent"] is True
    assert ur.verify_report(body, env["signature"], "rk-abc") is True
    # A forger trying to hide an event — drop a chain entry and adjust the counts —
    # alters the signed body, so the T3 signature no longer verifies.
    forged = json.loads(json.dumps(body))
    forged["tamper_evidence"]["chain"] = forged["tamper_evidence"]["chain"][:1]
    forged["tamper_evidence"]["event_count"] = 1
    forged["tamper_evidence"]["chain_root"] = forged["tamper_evidence"]["chain"][0]["chain_hash"]
    assert ur.verify_report(forged, env["signature"], "rk-abc") is False


# ---------------------------------------------------------------------------
# Per-org seq counter — monotonic + isolated (contract Postgres → CI)
# ---------------------------------------------------------------------------
def test_next_seq_is_monotonic_per_org(client):
    import uuid

    org = f"seqorg_{uuid.uuid4().hex[:8]}"
    first = bc.next_seq(org)
    second = bc.next_seq(org)
    third = bc.next_seq(org)
    assert (first, second, third) == (1, 2, 3)
    # A different org has its own independent sequence.
    other = f"seqorg_{uuid.uuid4().hex[:8]}"
    assert bc.next_seq(other) == 1
