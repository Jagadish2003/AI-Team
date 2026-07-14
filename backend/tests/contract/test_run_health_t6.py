"""Contract tests — R18-C2 T6: Run-Health Dashboard contract coverage per Section 4.

These tests complete the T6 suite the story asks for, proving the dashboard
satisfies the document across the states T1's tests did not yet exercise from
real seeded records:

* Connectors (AC1): distinct connected states — healthy, expired-auth,
  missing-auth, failed-health-check — each returning state, last successful
  ingestion, checkpoint age, authentication mode, and latest error.
* Runs (AC2): a non-blocking stage failure is reported as degraded with the
  stage and reason visible; a fully successful run stays successful; a failed
  run is failed; detector counts surface.
* Content (AC3): indexed volume per source, embedding backlog, stale chunks,
  backfill progress, skipped-with-reason records, and redaction counts — read
  from the REAL freshness/ingestion stores and telemetry, not mocked totals.
* Attention + tenancy (AC4/AC5): seeded expired-auth and stalled-checkpoint
  conditions, severity ordering, panel links, and a two-org proof that neither
  org sees the other's connectors, runs, content, packs, or attention items.
* Roles (AC6/AC7): Owners can read every panel; Analysts get a read-only
  dashboard (200 on reads); Viewers are denied (403); unauthenticated is 401 —
  across ALL five endpoints (content included). AC7: an owner can determine
  their tenant's health from these reads alone, no server logs / engineering.

Every endpoint is exercised through the real HTTP routes against the real
records. Org is taken from the request context; role is the workspace_members
row for the dev token.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app import db
from app.rbac import seed_owner
from app.telemetry import record_event

_DEV_TOKEN = "dev-token-change-me"


# ── helpers (mirror test_run_health.py so the two read the same) ───────────────

def _auth(org_id: str) -> dict:
    return {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org_id}


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _owner_org(prefix: str) -> str:
    org_id = f"{prefix}_{uuid4().hex[:8]}"
    seed_owner(org_id, _DEV_TOKEN)
    return org_id


def _set_role(org_id: str, role: str) -> dict:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            (org_id, _DEV_TOKEN, role, _now_iso()),
        )
        con.commit()
    finally:
        con.close()
    return _auth(org_id)


def _seed_connector_record(org_id: str, connector_id: str, *, status: str,
                           name: str | None = None, tier: str = "primary") -> None:
    """Seed this org's connector state row (drives connection_state)."""
    db.org_connector_set(
        org_id,
        connector_id,
        {
            "id": connector_id,
            "name": name or connector_id.title(),
            "tier": tier,
            "status": status,
            "configured": True,
        },
    )


def _seed_checkpoint(org_id: str, connector_id: str, value: str, captured_at: str) -> None:
    try:
        from discovery.ingest.base import Checkpoint
        from discovery.ingest.checkpoint_repository import save_checkpoint
    except ModuleNotFoundError:  # pragma: no cover
        from backend.discovery.ingest.base import Checkpoint
        from backend.discovery.ingest.checkpoint_repository import save_checkpoint
    save_checkpoint(
        Checkpoint(connector_id=connector_id, org_id=org_id, value=value, captured_at=captured_at)
    )


def _seed_oauth_token(org_id: str, connector_id: str, *, expires_offset: int, refresh: bool) -> None:
    from app.auth.vault import store_token

    token = {
        "access_token": f"tok-{uuid4().hex}",
        "expires_at": _now_iso(expires_offset),
        "scope": "read",
    }
    if refresh:
        token["refresh_token"] = f"refresh-{uuid4().hex}"
    store_token(org_id, connector_id, token)


def _seed_static_credential(org_id: str, connector_id: str) -> None:
    from app.auth.vault import store_static_credential

    store_static_credential(
        org_id,
        connector_id,
        username="svc-account",
        secret=f"secret-{uuid4().hex}",
        base_url="https://example.service-now.com",
    )


def _seed_health_check(org_id: str, connector_id: str, status: str) -> None:
    record_event(
        "connector.health_check",
        {
            "org_id": org_id,
            "connector_id": connector_id,
            "status": status,
            "message": f"health check reported {status}",
            "token_expiry_seconds": None,
            "check_duration_ms": 5,
        },
    )


def _seed_ingest_completed(org_id: str, connector_id: str, *, degraded: int = 0) -> None:
    record_event(
        "db.ingestor_completed",
        {
            "org_id": org_id,
            "connector_id": connector_id,
            "pack_id": "ncino",
            "query_count": 3,
            "signal_count": 10,
            "degraded_count": degraded,
            "duration_ms": 120,
        },
    )


def _seed_run(org_id: str, *, status: str, pack_id: str = "ncino", errors=None,
              opps: int = 0, started_offset: int = -120) -> str:
    run_id = f"run_{uuid4().hex[:10]}"
    run_payload = {
        "id": run_id,
        "org_id": org_id,
        "orgId": org_id,
        "status": status,
        "startedAt": _now_iso(started_offset),
        "updatedAt": _now_iso(),
        "packId": pack_id,
        "packName": "nCino Lending",
        "packVersion": "seeded-1.0.1",
        "executedDetectorIds": ["LOAN_ROUTING", "COVENANT_TRACKING"],
        "packExecutedAt": _now_iso(-30),
        "selectedSystemIds": ["servicenow", "jira"],
        "systemCount": 2,
        "source": "stack_builder",
    }
    db.upsert_run(
        run_id,
        run_payload,
    )
    db.run_kv_set(
        "status",
        run_id,
        {
            "runId": run_id,
            "status": status,
            "counts": {"opportunities": opps, "evidence": 0},
            "errors": errors or {},
            "updatedAt": _now_iso(),
        },
    )
    if opps:
        db.run_kv_set("opps", run_id, [{"id": f"opp_{i}"} for i in range(opps)])
    return run_id


def _retrieval_store_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_chunks')")
            return cur.fetchone()[0] is not None
        finally:
            con.close()
    except Exception:
        return False


def _seed_chunk(org_id: str, source_system: str, artifact: str, *,
                embedded: bool = False, stale: bool = False) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        embedding = "[" + ",".join(["0.1"] * 8) + "]" if embedded else None
        cur.execute(
            "INSERT INTO retrieval_chunks "
            "(chunk_id, org_id, content, content_hash, content_type, source_system, "
            " source_artifact, chunk_position, embedding, embedding_model, "
            " embedding_model_version, is_stale, stale_at, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())",
            (
                uuid4().hex, org_id, "content", uuid4().hex[:16], "prose",
                source_system, artifact, 0,
                embedding,
                "test-model" if embedded else None,
                "v1" if embedded else None,
                stale,
                _now_iso() if stale else None,
            ),
        )
        con.commit()
    finally:
        con.close()


def _cleanup_chunks(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


def _connectors(client, org_id: str) -> dict:
    return {c["connector_id"]: c for c in
            client.get("/api/run-health/connectors", headers=_auth(org_id)).json()["connectors"]}


# ── AC1: connectors — every distinct connected state ───────────────────────────

def test_healthy_connector_reports_full_state(client):
    """A healthy connector reports state, last ingestion, checkpoint age, auth
    mode, and (no) error — the full AC1 shape from real records."""
    org = _owner_org("t6_healthy")
    _seed_connector_record(org, "servicenow", status="connected")
    _seed_oauth_token(org, "servicenow", expires_offset=3600, refresh=True)
    _seed_checkpoint(org, "servicenow", "cursor-99", _now_iso(-1800))
    _seed_ingest_completed(org, "servicenow", degraded=0)

    conns = _connectors(client, org)
    sn = conns["servicenow"]
    assert sn["connection_state"] == "connected"
    assert sn["auth_mode"] == "oauth"
    assert sn["checkpoint_position"] == "cursor-99"
    assert sn["checkpoint_age_seconds"] is not None and sn["checkpoint_age_seconds"] >= 1700
    assert sn["last_successful_ingestion"] is not None
    assert sn["last_error"] is None


def test_expired_auth_connector_surfaces_state_and_auth_mode(client):
    """An expired-auth connector shows the auth-needing state and its auth mode."""
    org = _owner_org("t6_expired")
    _seed_connector_record(org, "salesforce", status="needs_auth")
    _seed_oauth_token(org, "salesforce", expires_offset=-3600, refresh=False)

    sf = _connectors(client, org)["salesforce"]
    assert sf["connection_state"] == "needs_auth"
    assert sf["auth_mode"] == "oauth"
    # And the attention strip flags it as expired authentication (critical).
    attention = client.get("/api/run-health/attention", headers=_auth(org)).json()["items"]
    auth_item = next(i for i in attention if i["connector_id"] == "salesforce")
    assert auth_item["condition"] == "expired_authentication"
    assert auth_item["severity"] == "critical"


def test_missing_auth_connector_has_no_auth_mode(client):
    """A connector configured but with no stored credential reports auth_mode None
    (missing auth) while still appearing with its state."""
    org = _owner_org("t6_missing")
    _seed_connector_record(org, "jira", status="needs_auth")
    # No credential stored → missing auth.
    jira = _connectors(client, org)["jira"]
    assert jira["connection_state"] == "needs_auth"
    assert jira["auth_mode"] is None


def test_failed_health_check_connector_surfaces_last_error(client):
    """A failed health check populates last_error, and the auth attention branch
    escalates a refresh_failed health event to unusable_authentication."""
    org = _owner_org("t6_failed")
    _seed_connector_record(org, "servicenow", status="refresh_failed")
    _seed_static_credential(org, "servicenow")
    _seed_health_check(org, "servicenow", "refresh_failed")

    sn = _connectors(client, org)["servicenow"]
    assert sn["connection_state"] == "refresh_failed"
    assert sn["auth_mode"] == "static"
    assert sn["last_error"] is not None and "refresh_failed" in sn["last_error"]

    attention = client.get("/api/run-health/attention", headers=_auth(org)).json()["items"]
    auth_item = next(i for i in attention if i["connector_id"] == "servicenow")
    assert auth_item["condition"] == "unusable_authentication"
    assert auth_item["severity"] == "critical"


def test_degraded_ingestion_reports_last_error(client):
    """A completed ingestion that degraded records surfaces as last_error."""
    org = _owner_org("t6_degraded_ingest")
    _seed_connector_record(org, "servicenow", status="connected")
    _seed_ingest_completed(org, "servicenow", degraded=4)

    sn = _connectors(client, org)["servicenow"]
    assert sn["last_error"] is not None and "degraded" in sn["last_error"].lower()


# ── AC2: runs — degraded / successful / failed / detector counts ────────────────

def test_failed_run_is_failed_not_degraded(client):
    org = _owner_org("t6_failed_run")
    _seed_run(org, status="failed")
    runs = client.get("/api/run-health/runs", headers=_auth(org)).json()["runs"]
    assert runs[0]["health_status"] == "failed"


def test_successful_run_stays_successful_alongside_a_degraded_one(client):
    """A fully successful run is still surfaced as healthy even when the org also
    has a degraded run — degradation is not smeared across all runs."""
    org = _owner_org("t6_mixed_runs")
    _seed_run(org, status="complete", errors={}, opps=2, started_offset=-60)
    _seed_run(org, status="complete", errors={"roadmap": "boom"}, opps=1, started_offset=-30)

    runs = client.get("/api/run-health/runs", headers=_auth(org)).json()["runs"]
    by_status = sorted(r["health_status"] for r in runs)
    assert by_status == ["degraded", "healthy"]
    degraded = next(r for r in runs if r["health_status"] == "degraded")
    assert any(s["stage"] == "roadmap" and "boom" in s["reason"] for s in degraded["degraded_stages"])


def test_run_detector_counts_from_signal_snapshot(client):
    org = _owner_org("t6_detectors")
    run_id = _seed_run(org, status="complete", opps=1)
    record_event(
        "run.signal_snapshot",
        {"org_id": org, "run_id": run_id, "detector_count": 5, "fired_count": 2},
    )
    run = client.get("/api/run-health/runs", headers=_auth(org)).json()["runs"][0]
    assert run["detectors_evaluated"] == 5
    assert run["detectors_fired"] == 2


# ── AC3: content — from REAL seeded stores, not mocked totals ────────────────────

@pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)
def test_content_reads_indexed_backlog_stale_from_store(client):
    org = _owner_org("t6_content")
    _cleanup_chunks(org)
    # 2 embedded document chunks, 1 pending git chunk, 1 stale document chunk.
    _seed_chunk(org, "document", "doc-1", embedded=True)
    _seed_chunk(org, "document", "doc-2", embedded=True)
    _seed_chunk(org, "git", "repo-1", embedded=False)
    _seed_chunk(org, "document", "doc-3", embedded=True, stale=True)
    try:
        body = client.get("/api/run-health/content", headers=_auth(org)).json()

        by_source = {r["source_system"]: r for r in body["indexed_by_source"]}
        assert by_source["document"]["chunk_count"] == 3
        assert by_source["git"]["chunk_count"] == 1

        assert body["chunks_total"] == 4
        assert body["chunks_embedded"] == 3          # three carry a vector
        assert body["pending_embeddings"] == 1       # the git chunk
        assert body["stale_chunks"] == 1             # the stale document chunk
        assert isinstance(body["backfill"], dict)
    finally:
        _cleanup_chunks(org)


def test_content_reports_skipped_with_reason_and_redaction(client):
    """Skipped-with-reason and redaction counts come from origin telemetry, live."""
    org = _owner_org("t6_content_events")
    record_event("ingestion.artifact_skipped",
                 {"org_id": org, "connector_id": "documents", "reason": "encrypted", "count": 1})
    record_event("ingestion.artifact_skipped",
                 {"org_id": org, "connector_id": "documents", "reason": "unsupported_format", "count": 2})
    record_event("ingestion.artifact_skipped",
                 {"org_id": org, "connector_id": "documents", "reason": "encrypted", "count": 1})
    record_event("ingestion.secret_redacted",
                 {"org_id": org, "connector_id": "git_content", "redaction_count": 3})
    record_event("ingestion.secret_redacted",
                 {"org_id": org, "connector_id": "git_content", "redaction_count": 2})

    body = client.get("/api/run-health/content", headers=_auth(org)).json()

    skipped = {s["reason"]: s["count"] for s in body["skipped"]}
    assert skipped.get("encrypted") == 2          # two events grouped
    assert skipped.get("unsupported_format") == 2
    # Grouped and sorted by reason (deterministic contract).
    assert [s["reason"] for s in body["skipped"]] == sorted(skipped)
    assert body["redaction_count"] == 5           # 3 + 2 summed


# ── AC4: attention — severity ordering across a mix of conditions ───────────────

def test_attention_orders_critical_high_across_conditions(client):
    """Expired auth (critical) sorts above a stalled checkpoint (high), each
    linking to its panel."""
    org = _owner_org("t6_attention_mix")
    _seed_connector_record(org, "salesforce", status="needs_auth")
    _seed_oauth_token(org, "salesforce", expires_offset=-3600, refresh=False)
    _seed_checkpoint(org, "servicenow", "stalled", _now_iso(-(3 * 24 * 60 * 60)))

    body = client.get("/api/run-health/attention", headers=_auth(org)).json()
    assert body["severity_order"] == ["critical", "high", "medium", "low"]

    conditions = [i["condition"] for i in body["items"]]
    severities = [i["severity"] for i in body["items"]]
    # Critical precedes high.
    assert severities == sorted(severities, key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}[s])
    assert "expired_authentication" in conditions
    assert "stalled_checkpoint" in conditions

    for item in body["items"]:
        assert item["panel"] in ("connectors", "runs", "content")
        assert item["href"].startswith("/run-health?panel=")


# ── AC5: two-org isolation — including content ─────────────────────────────────

@pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)
def test_content_is_org_scoped_across_two_orgs(client):
    org_a = _owner_org("t6_iso_a")
    org_b = _owner_org("t6_iso_b")
    _cleanup_chunks(org_a)
    _cleanup_chunks(org_b)
    _seed_chunk(org_a, "document", "a-doc", embedded=True)
    record_event("ingestion.secret_redacted", {"org_id": org_a, "redaction_count": 4})
    record_event("ingestion.artifact_skipped",
                 {"org_id": org_a, "reason": "encrypted", "count": 1})
    try:
        a = client.get("/api/run-health/content", headers=_auth(org_a)).json()
        b = client.get("/api/run-health/content", headers=_auth(org_b)).json()

        assert a["chunks_total"] == 1
        assert a["redaction_count"] == 4
        assert any(s["reason"] == "encrypted" for s in a["skipped"])

        # Org B sees NONE of org A's content, redactions, or skips.
        assert b["chunks_total"] == 0
        assert b["indexed_by_source"] == []
        assert b["redaction_count"] == 0
        assert b["skipped"] == []
    finally:
        _cleanup_chunks(org_a)
        _cleanup_chunks(org_b)


def test_connectors_and_attention_isolated_across_two_orgs(client):
    org_a = _owner_org("t6_iso2_a")
    org_b = _owner_org("t6_iso2_b")
    _seed_connector_record(org_a, "salesforce", status="needs_auth")
    _seed_oauth_token(org_a, "salesforce", expires_offset=-3600, refresh=False)
    _seed_checkpoint(org_a, "servicenow", "a-cursor", _now_iso(-60))

    assert _connectors(client, org_b) == {}
    b_attention = client.get("/api/run-health/attention", headers=_auth(org_b)).json()["items"]
    assert b_attention == []
    # Org A still sees its own.
    assert "salesforce" in _connectors(client, org_a)


# ── AC6/AC7: roles across ALL FIVE endpoints (content included) ─────────────────

_ALL_ENDPOINTS = [
    "/api/run-health/connectors",
    "/api/run-health/runs",
    "/api/run-health/content",
    "/api/run-health/packs",
    "/api/run-health/attention",
]


@pytest.mark.parametrize("path", _ALL_ENDPOINTS)
def test_unauthenticated_is_401_all_endpoints(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", _ALL_ENDPOINTS)
def test_viewer_is_forbidden_all_endpoints(client, path):
    headers = _set_role(f"t6_view_{uuid4().hex[:8]}", "viewer")
    assert client.get(path, headers=headers).status_code == 403


@pytest.mark.parametrize("path", _ALL_ENDPOINTS)
def test_analyst_read_only_all_endpoints(client, path):
    headers = _set_role(f"t6_anl_{uuid4().hex[:8]}", "analyst")
    assert client.get(path, headers=headers).status_code == 200


@pytest.mark.parametrize("path", _ALL_ENDPOINTS)
def test_owner_can_read_all_endpoints(client, path):
    """AC7: an owner can determine their tenant's health from every panel read
    (no server logs / engineering support)."""
    headers = _set_role(f"t6_own_{uuid4().hex[:8]}", "owner")
    assert client.get(path, headers=headers).status_code == 200


def test_owner_determines_tenant_health_end_to_end(client):
    """AC7 end-to-end: with one seeded degradation of each kind, an owner reading
    the five endpoints alone can see connector auth trouble, a degraded run, a
    content skip, and a severity-ordered attention list telling them what to do."""
    org = f"t6_ac7_{uuid4().hex[:8]}"
    headers = _set_role(org, "owner")
    _seed_connector_record(org, "salesforce", status="needs_auth")
    _seed_oauth_token(org, "salesforce", expires_offset=-3600, refresh=False)
    _seed_run(org, status="complete", errors={"roadmap": "roadmap engine raised: boom"}, opps=1)
    record_event("ingestion.artifact_skipped",
                 {"org_id": org, "connector_id": "documents", "reason": "encrypted", "count": 1})
    connectors = client.get("/api/run-health/connectors", headers=headers).json()["connectors"]
    runs = client.get("/api/run-health/runs", headers=headers).json()["runs"]
    content = client.get("/api/run-health/content", headers=headers).json()
    attention = client.get("/api/run-health/attention", headers=headers).json()

    # Connector auth trouble is visible.
    assert any(c["connector_id"] == "salesforce" and c["connection_state"] == "needs_auth"
               for c in connectors)
    # The degraded run names its failing stage and reason.
    degraded = next(r for r in runs if r["health_status"] == "degraded")
    assert any("roadmap" == s["stage"] and "boom" in s["reason"] for s in degraded["degraded_stages"])
    # The content skip is visible.
    assert any(s["reason"] == "encrypted" for s in content["skipped"])
    # The attention strip tells the owner what to act on, severity-ordered, linked.
    assert attention["items"], "attention strip should list the seeded conditions"
    top = attention["items"][0]
    assert top["condition"] == "expired_authentication"  # critical sorts first
    assert top["href"].startswith("/run-health?panel=connectors")
