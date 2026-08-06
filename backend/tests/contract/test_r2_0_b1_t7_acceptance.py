"""2.0-B1 T7 — QA: acceptance criteria validated end to end over a live run.

The DB-free half of this validation lives in
``tests/unit/test_r2_0_b1_t7_acceptance.py``. This file drives the REAL HTTP
routes, the real tenancy/RBAC middleware, the real run-scoped storage, and the
real ``audit_log`` table over a materialized offline discovery run — the closest
thing to walking the story with a customer:

  AC1 — any finding expands to a complete chain terminating in source records;
        every hop carries origin, connector, run id, and timestamp.
  AC2 — joined claims display the join type and correlation window used; a claim
        whose join is outside window cannot appear (MSP-B7 regression).
  AC3 — the trace shows retrieval candidates both used and not used by assembly.
  AC4 — the export bundle verifies against its signature; altering any byte fails.
  AC5 — exports carry no unredacted secrets and no host x vulnerability
        enumeration (the 1.9 aggregation floor holds in export).
  AC6 — every export generation is an audit event naming user, scope, and time —
        asserted against the rows the product actually wrote.

Mutating tests that seed content into a run's storage restore it afterwards, so
the shared module-scoped run stays usable by the tests that follow.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app import evidence_export as ee

REPORT_KEY = "rk-t7-acceptance-contract"


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


def _dev_user() -> str:
    return os.getenv("DEV_JWT", "dev-token-change-me")


@pytest.fixture
def signing_key(monkeypatch):
    """A test install carries no issued license — give it a report_key so a
    bundle can be signed. (The unsigned-refusal path is asserted in
    tests/contract/test_evidence_export_contract.py.)"""
    monkeypatch.setattr(
        "app.usage_report._resolve_license_signing",
        lambda org_id: (REPORT_KEY, "cf-2026-1", org_id),
    )


@pytest.fixture(scope="module")
def qa_run_id(client: TestClient):
    """A dedicated offline run for this suite, so its storage can be seeded and
    restored without disturbing the other export/trace suites."""
    body = {
        "connectedSources": ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles": [], "sampleWorkspaceEnabled": False,
        "mode": "offline", "systems": ["salesforce", "servicenow", "jira"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201), f"start failed: {r.text}"
    run_id = r.json().get("runId") or r.json().get("id")
    assert run_id

    status = "running"
    for _ in range(90):
        st = client.get(f"/api/runs/{run_id}/status", headers=_auth())
        if st.status_code == 200:
            status = st.json().get("status", "running")
            if status in ("complete", "partial", "failed"):
                break
        time.sleep(1)
    assert status in ("complete", "partial"), f"run reached '{status}'"
    return run_id


@pytest.fixture(scope="module")
def qa_opp_id(client: TestClient, qa_run_id):
    r = client.get(f"/api/runs/{qa_run_id}/opportunities", headers=_auth())
    assert r.status_code == 200 and r.json()
    return r.json()[0]["id"]


@pytest.fixture
def restore_run_kv(qa_run_id):
    """Snapshot the run-scoped keys a test seeds, and put them back afterwards."""
    from app import db

    saved: Dict[str, Any] = {}

    def snapshot(*keys: str) -> None:
        for key in keys:
            saved[key] = db.run_kv_get(key, qa_run_id, None)

    yield snapshot

    for key, value in saved.items():
        db.run_kv_set(key, qa_run_id, value)


def _trace(client: TestClient, run_id: str, opp_id: str) -> Dict[str, Any]:
    r = client.get(
        f"/api/runs/{run_id}/opportunities/{opp_id}/trace-graph", headers=_auth()
    )
    assert r.status_code == 200, r.text
    return r.json()


def _audit_events(
    event_type: str, org_id: str = "default"
) -> List[Dict[str, Any]]:
    """Every audit row of one type for the org, newest-first.

    Read straight from ``audit_log`` (the ``test_audit.py`` helper's pattern)
    rather than through ``/api/audit-log``: that viewer serves a page of the whole
    org-wide stream, and the shared contract session writes thousands of unrelated
    rows, so a paged read cannot support exact before/after counting. The viewer
    surface itself is asserted separately in
    :func:`test_ac6_the_export_audit_row_is_visible_through_the_audit_viewer`.
    """
    from app import db

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, org_id, event_type, user_id, run_id, connector_id, payload, "
            "timestamp FROM audit_log WHERE org_id = %s AND event_type = %s "
            "ORDER BY timestamp DESC",
            (org_id, event_type),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [
        {
            "id": r[0], "org_id": r[1], "event_type": r[2], "user_id": r[3],
            "run_id": r[4], "connector_id": r[5],
            "payload": json.loads(r[6]) if r[6] else None,
            "timestamp": r[7],
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — a complete, navigable chain terminating in source records
# ─────────────────────────────────────────────────────────────────────────────


def test_ac1_a_real_finding_expands_to_a_complete_chain(client: TestClient, qa_run_id, qa_opp_id):
    data = _trace(client, qa_run_id, qa_opp_id)
    assert data["available"] is True
    assert data["complete"] is True
    assert len(data["hops"]) >= 2

    hop_types = [h["hop_type"] for h in data["hops"]]
    assert hop_types.count("finding") == 1
    assert "source_record" in hop_types or "evidence" in hop_types

    for hop in data["hops"]:
        for key in ("hop_id", "hop_type", "origin", "connector", "run_id",
                    "timestamp", "from_hop_id"):
            assert key in hop, f"{hop.get('hop_id')} is missing {key}"
        assert hop["origin"] in ("observed", "inferred")
        assert hop["run_id"] == qa_run_id


def test_ac1_the_chain_is_navigable_from_the_finding_to_every_leaf(
    client: TestClient, qa_run_id, qa_opp_id
):
    """An orphan hop is not interrogable — the whole point of AC1 is that a
    reviewer can walk from the claim to the record."""
    data = _trace(client, qa_run_id, qa_opp_id)
    hop_ids = {h["hop_id"] for h in data["hops"]}
    roots = [h for h in data["hops"] if h["from_hop_id"] is None]
    assert len(roots) == 1 and roots[0]["hop_type"] == "finding"
    for hop in data["hops"]:
        if hop["from_hop_id"] is None:
            continue
        assert hop["from_hop_id"] in hop_ids, f"{hop['hop_id']} is orphaned"


def test_ac1_every_finding_in_the_run_has_a_chain(client: TestClient, qa_run_id):
    """"Any finding expands" — not just the first one."""
    opps = client.get(f"/api/runs/{qa_run_id}/opportunities", headers=_auth()).json()
    assert opps
    for opp in opps[:10]:
        data = _trace(client, qa_run_id, opp["id"])
        assert data["available"] is True, opp["id"]
        assert len(data["hops"]) >= 2, opp["id"]


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — join type + window shown; an out-of-window join cannot appear
# ─────────────────────────────────────────────────────────────────────────────


def test_ac2_no_out_of_window_join_is_ever_served(client: TestClient, qa_run_id):
    """Property over the whole run: whatever joins a finding carries, every one
    of them is within its window and names its join type and window."""
    opps = client.get(f"/api/runs/{qa_run_id}/opportunities", headers=_auth()).json()
    for opp in opps[:10]:
        data = _trace(client, qa_run_id, opp["id"])
        assert isinstance(data["joins"], list)
        for join in data["joins"]:
            assert join["within_window"] is True
            assert join["join_type"]
            assert join["window_seconds"] is not None


def test_ac2_a_mixed_join_set_serves_only_the_within_window_claim(
    client: TestClient, qa_run_id, qa_opp_id, monkeypatch
):
    """End to end through the real route: a finding whose contract carries both an
    in-window and an out-of-window join surfaces exactly one — the in-window one.
    (Seeded at the trace-engine seam because a fixture-driven offline run does not
    produce cloud-event joins of its own.)"""
    from app.trace_graph import build_finding_trace

    def _window(within: bool, delta: float) -> Dict[str, Any]:
        return {
            "join_type": "event_incident", "window_seconds": 7200,
            "delta_seconds": delta, "within_window": within,
            "a_at": "2026-07-30T09:00:00+00:00", "b_at": "2026-07-30T09:30:00+00:00",
        }

    opportunity = {
        "id": qa_opp_id,
        "title": "Seeded joined claim",
        "evidenceIds": [],
        "findingContract": {
            "corroboration": {
                "status": "corroborated",
                "sources": ["servicenow", "events"],
                "window_gated": True,
                "correlation_windows": [
                    _window(True, 900.0),
                    _window(False, 90000.0),
                    {"join_type": "event_event", "within_window": "false"},
                ],
            },
            "source_trace": {
                "systems": ["servicenow", "events"],
                "artifacts": [{"type": "event_signature", "id": "v1:seed"}],
            },
        },
    }
    monkeypatch.setattr(
        "app.trace_graph.load_finding_trace",
        lambda run_id, opp_id: build_finding_trace(opportunity, run_id),
    )

    data = _trace(client, qa_run_id, qa_opp_id)
    assert len(data["joins"]) == 1
    join = data["joins"][0]
    assert join["within_window"] is True
    assert join["join_type"] == "event_incident"
    assert join["window_seconds"] == 7200
    assert join["delta_seconds"] == 900.0
    assert "event_event" not in json.dumps(data["joins"])


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — retrieval candidates used AND unused are both served
# ─────────────────────────────────────────────────────────────────────────────


def test_ac3_stored_candidates_round_trip_to_the_trace_used_and_unused(
    client: TestClient, qa_run_id, qa_opp_id, restore_run_kv
):
    """Persist a mixed candidate set exactly as the assembly hook does, then read
    it back through the API: both sides of "retrieval proposes, assembly decides"
    must be visible, with reasons."""
    from app.retrieval_trace import store_retrieval_candidates

    restore_run_kv("retrieval_candidates")
    store_retrieval_candidates(qa_run_id, qa_opp_id, [
        {"chunk_id": "qa_used_1", "used": True, "decision": "included",
         "reason": "included@position_1", "confidence": 0.94, "origin": "observed",
         "source_system": "confluence", "source_artifact": "runbook-page-7",
         "content_snippet": "step 3: restart the worker", "is_stale": False},
        {"chunk_id": "qa_unused_floor", "used": False, "decision": "excluded",
         "reason": "below_confidence_floor", "confidence": 0.01, "origin": "observed",
         "source_system": "git", "source_artifact": "README.md",
         "content_snippet": "unrelated", "is_stale": False},
        {"chunk_id": "qa_unused_stale", "used": False, "decision": "excluded",
         "reason": "stale", "confidence": 0.9, "origin": "observed",
         "source_system": "sharepoint", "source_artifact": "policy.docx",
         "content_snippet": "superseded", "is_stale": True},
    ])

    data = _trace(client, qa_run_id, qa_opp_id)
    assert data["retrieval_candidates_used_count"] == 1
    assert data["retrieval_candidates_unused_count"] == 2
    assert len(data["retrieval_candidates"]) == 3

    by_id = {c["chunk_id"]: c for c in data["retrieval_candidates"]}
    assert by_id["qa_used_1"]["used"] is True
    assert by_id["qa_used_1"]["source_artifact"] == "runbook-page-7"
    assert by_id["qa_unused_floor"]["used"] is False
    assert by_id["qa_unused_floor"]["reason"] == "below_confidence_floor"
    assert by_id["qa_unused_stale"]["is_stale"] is True


def test_ac3_the_candidate_surface_is_always_present(client: TestClient, qa_run_id, qa_opp_id):
    """Always present, sometimes empty — never a missing field the UI must guess
    about, and the counts always reconcile with the list."""
    data = _trace(client, qa_run_id, qa_opp_id)
    assert isinstance(data["retrieval_candidates"], list)
    assert (
        data["retrieval_candidates_used_count"]
        + data["retrieval_candidates_unused_count"]
        == len(data["retrieval_candidates"])
    )


def test_ac3_the_export_bundle_carries_the_same_used_unused_record(
    client: TestClient, qa_run_id, qa_opp_id, restore_run_kv, signing_key
):
    """The assembly decision has to survive into the auditable artifact, not just
    the screen."""
    from app.retrieval_trace import store_retrieval_candidates

    restore_run_kv("retrieval_candidates")
    store_retrieval_candidates(qa_run_id, qa_opp_id, [
        {"chunk_id": "qa_used_1", "used": True, "decision": "included",
         "reason": "included@position_1", "confidence": 0.94, "origin": "observed",
         "source_system": "confluence", "source_artifact": "runbook-page-7",
         "content_snippet": "step 3", "is_stale": False},
        {"chunk_id": "qa_unused_1", "used": False, "decision": "excluded",
         "reason": "ranked_out", "confidence": 0.3, "origin": "observed",
         "source_system": "slack", "source_artifact": "thread-1",
         "content_snippet": "chatter", "is_stale": False},
    ])

    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    trace = r.json()["bundle"]["findings"][0]["trace"]
    assert trace["retrieval_candidates_used_count"] == 1
    assert trace["retrieval_candidates_unused_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — the served bundle verifies; altering any byte fails
# ─────────────────────────────────────────────────────────────────────────────


def test_ac4_the_bundle_the_route_serves_verifies(
    client: TestClient, qa_run_id, qa_opp_id, signing_key
):
    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    envelope = r.json()
    assert envelope["algorithm"] == ee.SIGNATURE_ALGORITHM
    assert ee.verify_export_envelope(envelope, REPORT_KEY)["verified"] is True

    tampered = json.loads(json.dumps(envelope))
    tampered["bundle"]["findings"][0]["opportunity"]["impact"] = 99
    assert ee.verify_export_envelope(tampered, REPORT_KEY)["verified"] is False


def test_ac4_the_downloaded_bytes_are_what_an_auditor_verifies(
    client: TestClient, qa_run_id, qa_opp_id, signing_key
):
    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers=_auth(), params={"download": "1"},
    )
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers.get("content-disposition", "")
    raw = r.content
    assert ee.verify_export_bytes(raw, REPORT_KEY)["verified"] is True

    flipped = bytearray(raw)
    index = raw.find(b'"run_id"')
    assert index > 0
    flipped[index + 2] = (flipped[index + 2] + 1) % 256
    assert ee.verify_export_bytes(bytes(flipped), REPORT_KEY)["verified"] is False


def test_ac4_the_report_scope_bundle_verifies(client: TestClient, qa_run_id, signing_key):
    r = client.get(f"/api/runs/{qa_run_id}/evidence-export", headers=_auth())
    assert r.status_code == 200, r.text
    envelope = r.json()
    assert envelope["bundle"]["scope"] == "report"
    assert ee.verify_export_envelope(envelope, REPORT_KEY)["verified"] is True


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — no unredacted secrets, no host x vulnerability enumeration
# ─────────────────────────────────────────────────────────────────────────────


SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def test_ac5_a_secret_seeded_into_a_run_never_reaches_the_export(
    client: TestClient, qa_run_id, qa_opp_id, restore_run_kv, signing_key
):
    from app import db

    restore_run_kv("evidence")
    evidence = db.run_kv_get("evidence", qa_run_id, []) or []
    assert evidence, "the run must have evidence to seed into"
    seeded = [dict(e) for e in evidence]
    seeded[0]["snippet"] = f"{seeded[0].get('snippet', '')} token {SECRET}"
    db.run_kv_set("evidence", qa_run_id, seeded)

    r = client.get(f"/api/runs/{qa_run_id}/evidence-export", headers=_auth())
    assert r.status_code == 200, r.text
    envelope = r.json()
    assert SECRET not in json.dumps(envelope)
    # ...and the redaction is declared on the signed bundle, not silent.
    assert envelope["bundle"]["redacted_pattern_types"]
    assert ee.verify_export_envelope(envelope, REPORT_KEY)["verified"] is True


def test_ac5_an_enumerating_run_cannot_be_exported_at_all(
    client: TestClient, qa_run_id, qa_opp_id, restore_run_kv, signing_key
):
    """The 1.9 aggregation floor holds in export: refused with a reason, never a
    signed host x vulnerability target list."""
    from app import db

    restore_run_kv("evidence")
    evidence = db.run_kv_get("evidence", qa_run_id, []) or []
    seeded = [dict(e) for e in evidence]
    seeded[0]["snippet"] = "Host 10.1.2.3 is affected by CVE-2026-1234."
    db.run_kv_set("evidence", qa_run_id, seeded)

    for path in (
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        f"/api/runs/{qa_run_id}/evidence-export",
    ):
        r = client.get(path, headers=_auth())
        assert r.status_code == 400, f"{path} -> {r.status_code}: {r.text}"
        assert "aggregation floor" in r.text
        assert "signature" not in r.json()


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — every export generation is an audit event naming user, scope, and time
# ─────────────────────────────────────────────────────────────────────────────


def _assert_export_audit_row(row: Dict[str, Any], *, expected_scope: str) -> None:
    """The three AC6 parts, as they land in ``audit_log``."""
    import datetime as dt

    payload = row.get("payload") or {}
    # user — the row's own actor column (log_event lifts user_id out of the
    # payload into that column, as it does for every audit event).
    assert row["user_id"] == _dev_user(), (
        f"export audit row must name the acting user, got {row['user_id']!r}"
    )
    # scope — WHAT was exported.
    assert payload["scope"] == expected_scope
    assert payload["export_kind"]
    # time — the row's column, plus the payload's own ISO-8601 UTC stamp so the
    # event carries its time even when the payload travels on its own.
    assert row["timestamp"]
    parsed = dt.datetime.fromisoformat(payload["timestamp"])
    assert parsed.tzinfo is not None
    # content discipline: identifiers/counts/hashes only.
    assert payload.get("content_root") or payload.get("period_from")
    assert len(str(payload.get("signature_prefix") or "")) <= 16


def test_ac6_a_finding_export_writes_an_audit_row_naming_user_scope_and_time(
    client: TestClient, qa_run_id, qa_opp_id, signing_key
):
    before = len(_audit_events("evidence_export_generated"))

    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    content_root = r.json()["bundle"]["integrity"]["content_root"]

    rows = _audit_events("evidence_export_generated")
    assert len(rows) == before + 1, "exactly one audit row per export generation"
    row = next(
        (row for row in rows if (row.get("payload") or {}).get("content_root") == content_root),
        None,
    )
    assert row is not None, "the audit row must identify the bundle that was issued"
    _assert_export_audit_row(row, expected_scope="finding")
    assert row["run_id"] == qa_run_id
    assert (row["payload"] or {}).get("opportunity_id") == qa_opp_id
    # The audit row never reproduces the MAC or any bundle content.
    assert r.json()["signature"] not in json.dumps(row)


def test_ac6_a_report_export_is_audited_with_report_scope(
    client: TestClient, qa_run_id, signing_key
):
    r = client.get(f"/api/runs/{qa_run_id}/evidence-export", headers=_auth())
    assert r.status_code == 200, r.text
    content_root = r.json()["bundle"]["integrity"]["content_root"]

    row = next(
        (
            row for row in _audit_events("evidence_export_generated")
            if (row.get("payload") or {}).get("content_root") == content_root
        ),
        None,
    )
    assert row is not None
    _assert_export_audit_row(row, expected_scope="report")


def test_ac6_the_download_form_is_audited_too(
    client: TestClient, qa_run_id, qa_opp_id, signing_key
):
    """The attachment is the copy that actually leaves the deployment."""
    before = len(_audit_events("evidence_export_generated"))
    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers=_auth(), params={"download": "1"},
    )
    assert r.status_code == 200, r.text
    assert len(_audit_events("evidence_export_generated")) == before + 1


def test_ac6_the_export_audit_row_is_visible_through_the_audit_viewer(
    client: TestClient, qa_run_id, qa_opp_id, signing_key
):
    """An audit record nobody can read is not an audit trail: the row must also
    reach the product's owner-only ``/api/audit-log`` viewer, actor and all."""
    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    content_root = r.json()["bundle"]["integrity"]["content_root"]

    viewer = client.get("/api/audit-log?limit=100", headers=_auth())
    assert viewer.status_code == 200, viewer.text
    row = next(
        (
            entry for entry in viewer.json()
            if entry.get("event_type") == "evidence_export_generated"
            and (entry.get("payload") or {}).get("content_root") == content_root
        ),
        None,
    )
    assert row is not None, "the export must be visible in the audit viewer"
    _assert_export_audit_row(row, expected_scope="finding")


def test_ac6_a_refused_export_is_not_recorded_as_one(
    client: TestClient, qa_run_id, qa_opp_id
):
    """No bundle was produced, so nothing was exported — a 400 must not appear in
    the trail as an issued attestation. (No ``signing_key`` fixture here: a test
    install carries no license report_key, so the export is refused.)"""
    before = len(_audit_events("evidence_export_generated"))
    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 400, r.text
    assert len(_audit_events("evidence_export_generated")) == before


def test_ac6_a_denied_export_is_not_recorded_as_one(
    client: TestClient, qa_run_id, qa_opp_id, signing_key
):
    before = len(_audit_events("evidence_export_generated"))
    viewer = os.getenv("VIEWER_JWT", "viewer-token")
    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert r.status_code == 403
    assert len(_audit_events("evidence_export_generated")) == before


def test_ac6_the_usage_report_export_is_audited(client: TestClient, signing_key):
    """Every export generation — including the other signed artifact the product
    hands out."""
    before = len(_audit_events("usage_report_exported"))
    r = client.get(
        "/api/usage/report", headers=_auth(),
        params={"from": "2026-07-01", "to": "2026-07-31"},
    )
    assert r.status_code == 200, r.text

    rows = _audit_events("usage_report_exported")
    assert len(rows) == before + 1
    row = rows[0]   # newest-first
    _assert_export_audit_row(row, expected_scope="usage_report")
    payload = row["payload"]
    assert payload["period_from"] == "2026-07-01"
    assert payload["period_to"] == "2026-07-31"
    assert r.json()["signature"] not in json.dumps(row)


def test_ac6_export_audit_rows_are_filed_under_the_exporting_org_only(
    client: TestClient, qa_run_id, qa_opp_id, signing_key
):
    """An export row must belong to the org that generated it — the audit viewer
    is org-scoped, so a row filed elsewhere is both invisible to its owner and
    visible to a stranger."""
    r = client.get(
        f"/api/runs/{qa_run_id}/opportunities/{qa_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    content_root = r.json()["bundle"]["integrity"]["content_root"]

    ours = _audit_events("evidence_export_generated", org_id="default")
    assert any((row.get("payload") or {}).get("content_root") == content_root for row in ours)

    for other_org in ("some_other_org", "_unattributed"):
        theirs = _audit_events("evidence_export_generated", org_id=other_org)
        assert not any(
            (row.get("payload") or {}).get("content_root") == content_root
            for row in theirs
        ), f"the export row must not be filed under {other_org!r}"
