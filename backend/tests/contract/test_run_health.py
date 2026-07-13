"""Contract tests — R18-C2 T1 Run-Health Dashboard aggregation endpoints.

Verifies the four read-only, org-scoped health endpoints against the real records
they assemble, through the real HTTP routes:

* ``GET /api/run-health/connectors`` — per-connector state incl. checkpoint (AC1).
* ``GET /api/run-health/runs``       — recent runs, non-blocking failure shown as
                                       degraded with stage + reason (AC2).
* ``GET /api/run-health/content``    — indexed volume per source, backlog, stale,
                                       skipped, redaction (AC3).
* ``GET /api/run-health/packs``      — latest run's packs + versions + detectors.
* ``GET /api/run-health/attention``  — deterministic actionable conditions (AC4).

Plus the boundary rules: org-scoping (AC5 — no cross-tenant visibility) and RBAC
(AC6 — Analyst read-only, Viewer forbidden, unauthenticated 401).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app import db
from app.rbac import seed_owner

_DEV_TOKEN = "dev-token-change-me"


# ── helpers ───────────────────────────────────────────────────────────────────

def _auth(org_id: str) -> dict:
    return {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org_id}


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _owner_org(prefix: str) -> str:
    org_id = f"{prefix}_{uuid4().hex[:8]}"
    seed_owner(org_id, _DEV_TOKEN)
    return org_id


def _set_role(org_id: str, role: str) -> dict:
    """Seed the dev token as ``role`` in ``org_id`` and return its headers."""
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


def _seed_checkpoint(org_id: str, connector_id: str, value: str, captured_at: str) -> None:
    try:
        from discovery.ingest.base import Checkpoint
        from discovery.ingest.checkpoint_repository import save_checkpoint
    except ModuleNotFoundError:  # pragma: no cover
        from backend.discovery.ingest.base import Checkpoint
        from backend.discovery.ingest.checkpoint_repository import save_checkpoint
    save_checkpoint(
        Checkpoint(
            connector_id=connector_id,
            org_id=org_id,
            value=value,
            captured_at=captured_at,
        )
    )


def _seed_expired_token(org_id: str, connector_id: str = "salesforce") -> str:
    from app.auth.vault import store_token

    expires_at = _now_iso(-3600)
    store_token(
        org_id,
        connector_id,
        {
            "access_token": f"expired-{uuid4().hex}",
            "expires_at": expires_at,
            "scope": "read",
        },
    )
    return expires_at


def _seed_run(org_id: str, *, status: str, pack_id: str = "ncino", errors=None,
              opps: int = 0, started_offset: int = -120) -> str:
    run_id = f"run_{uuid4().hex[:10]}"
    db.upsert_run(
        run_id,
        {
            "id": run_id,
            "org_id": org_id,
            "orgId": org_id,
            "status": status,
            "startedAt": _now_iso(started_offset),
            "updatedAt": _now_iso(),
            "packId": pack_id,
            "selectedSystemIds": ["servicenow", "jira"],
            "systemCount": 2,
            "source": "stack_builder",
        },
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


def _seed_chunk(org_id: str, source_system: str, artifact: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO retrieval_chunks "
            "(chunk_id, org_id, content, content_hash, content_type, source_system, "
            " source_artifact, chunk_position, is_stale, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())",
            (uuid4().hex, org_id, "content", "h" * 8, "prose",
             source_system, artifact, 0, False),
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


# ── AC1: connectors ────────────────────────────────────────────────────────────

def test_connectors_reports_connected_system_with_checkpoint(client):
    org = _owner_org("rh_conn")
    captured = _now_iso(-3600)  # one hour ago
    _seed_checkpoint(org, "servicenow", "cursor-42", captured)

    resp = client.get("/api/run-health/connectors", headers=_auth(org))
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_id"] == org

    sn = next((c for c in body["connectors"] if c["connector_id"] == "servicenow"), None)
    assert sn is not None, f"servicenow not in {[c['connector_id'] for c in body['connectors']]}"
    assert sn["checkpoint_position"] == "cursor-42"
    assert sn["checkpoint_captured_at"] is not None
    assert sn["checkpoint_age_seconds"] is not None and sn["checkpoint_age_seconds"] >= 3500
    # last_error/state/auth_mode keys are always present (AC1 shape).
    for key in ("connection_state", "auth_mode", "last_successful_ingestion", "last_error"):
        assert key in sn


# ── AC2: runs — non-blocking failure shown as degraded ──────────────────────────

def test_degraded_run_shows_stage_and_reason(client):
    org = _owner_org("rh_degr")
    _seed_run(
        org,
        status="complete",
        errors={"roadmap": "roadmap engine raised: boom"},
        opps=3,
    )

    resp = client.get("/api/run-health/runs", headers=_auth(org))
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1
    run = runs[0]

    # A non-blocking stage failure is NEVER an undifferentiated success (AC2).
    assert run["health_status"] == "degraded"
    assert run["degraded"] is True
    stages = {s["stage"]: s["reason"] for s in run["degraded_stages"]}
    assert "roadmap" in stages
    assert "boom" in stages["roadmap"]
    assert run["opportunities"] == 3
    assert run["pack_id"] == "ncino"
    assert run["duration_seconds"] is not None


def test_clean_run_is_healthy(client):
    org = _owner_org("rh_clean")
    _seed_run(org, status="complete", errors={}, opps=1)

    runs = client.get("/api/run-health/runs", headers=_auth(org)).json()["runs"]
    assert len(runs) == 1
    assert runs[0]["health_status"] == "healthy"
    assert runs[0]["degraded"] is False
    assert runs[0]["degraded_stages"] == []


def test_partial_run_is_degraded(client):
    org = _owner_org("rh_partial")
    _seed_run(org, status="partial", errors={}, opps=0)
    runs = client.get("/api/run-health/runs", headers=_auth(org)).json()["runs"]
    assert runs[0]["health_status"] == "degraded"


# ── packs ────────────────────────────────────────────────────────────────────

def test_packs_from_latest_run(client):
    org = _owner_org("rh_packs")
    _seed_run(org, status="complete", pack_id="ncino", opps=1)

    resp = client.get("/api/run-health/packs", headers=_auth(org))
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] is not None
    assert len(body["packs"]) == 1
    pack = body["packs"][0]
    assert pack["pack_id"] == "ncino"
    assert isinstance(pack["pack_version"], str) and pack["pack_version"]
    assert pack["detector_count"] > 0
    assert len(pack["detectors"]) == pack["detector_count"]
    assert all(isinstance(detector, str) and detector for detector in pack["detectors"])


def test_packs_empty_without_runs(client):
    org = _owner_org("rh_nopacks")
    body = client.get("/api/run-health/packs", headers=_auth(org)).json()
    assert body == {"run_id": None, "packs": []}


# ── AC3: content & freshness ────────────────────────────────────────────────────

@pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)
def test_content_indexed_by_source_and_shape(client):
    org = _owner_org("rh_content")
    _cleanup_chunks(org)
    _seed_chunk(org, "document", "doc-1")
    _seed_chunk(org, "document", "doc-2")
    _seed_chunk(org, "git", "repo-1")
    try:
        resp = client.get("/api/run-health/content", headers=_auth(org))
        assert resp.status_code == 200
        body = resp.json()

        # Indexed volume per source, live against the store (AC3).
        by_source = {r["source_system"]: r["chunk_count"] for r in body["indexed_by_source"]}
        assert by_source.get("document") == 2
        assert by_source.get("git") == 1

        # Backlog / stale / freshness shape present and live.
        assert body["chunks_total"] == 3
        assert body["pending_embeddings"] == 3  # none embedded
        assert body["stale_chunks"] == 0
        assert isinstance(body["backfill"], dict)
        assert isinstance(body["redaction_count"], int)
        assert isinstance(body["skipped"], list)
    finally:
        _cleanup_chunks(org)


# ── AC4: attention strip ─────────────────────────────────────────────────────

def test_attention_surfaces_expired_auth_before_stalled_checkpoint(client):
    org = _owner_org("rh_attention")
    expired_at = _seed_expired_token(org, "salesforce")
    checkpoint_at = _now_iso(-(2 * 24 * 60 * 60))
    _seed_checkpoint(org, "servicenow", "stalled-cursor", checkpoint_at)

    resp = client.get("/api/run-health/attention", headers=_auth(org))
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_id"] == org
    assert body["severity_order"] == ["critical", "high", "medium", "low"]

    items = body["items"]
    assert [item["condition"] for item in items] == [
        "expired_authentication",
        "stalled_checkpoint",
    ]
    assert [item["severity"] for item in items] == ["critical", "high"]

    auth_item, checkpoint_item = items
    assert auth_item["id"] == "auth:salesforce"
    assert auth_item["connector_id"] == "salesforce"
    assert auth_item["timestamp"] == expired_at
    assert auth_item["panel"] == "connectors"
    assert auth_item["href"] == "/run-health?panel=connectors&connector=salesforce"

    assert checkpoint_item["id"] == "checkpoint:servicenow"
    assert checkpoint_item["connector_id"] == "servicenow"
    assert checkpoint_item["panel"] == "connectors"
    assert checkpoint_item["href"].endswith("connector=servicenow")
    assert checkpoint_item["details"]["checkpoint_age_seconds"] >= 2 * 24 * 60 * 60

    required = {
        "id", "condition", "severity", "title", "explanation", "connector_id",
        "run_id", "timestamp", "panel", "href", "details",
    }
    assert all(required <= set(item) for item in items)


def test_attention_equal_severity_uses_time_then_identifier_tie_breaker(client):
    org = _owner_org("rh_attention_order")
    captured = _now_iso(-(2 * 24 * 60 * 60))
    _seed_checkpoint(org, "servicenow", "sn-cursor", captured)
    _seed_checkpoint(org, "jira", "jira-cursor", captured)

    first = client.get("/api/run-health/attention", headers=_auth(org)).json()["items"]
    second = client.get("/api/run-health/attention", headers=_auth(org)).json()["items"]
    expected = ["checkpoint:jira", "checkpoint:servicenow"]
    assert [item["id"] for item in first] == expected
    assert [item["id"] for item in second] == expected


def test_refreshable_expired_access_token_is_not_actionable(client):
    from app.auth.vault import store_token

    org = _owner_org("rh_attention_refreshable")
    store_token(
        org,
        "salesforce",
        {
            "access_token": "expired-but-refreshable",
            "refresh_token": "refresh-me",
            "expires_at": _now_iso(-3600),
            "scope": "read",
        },
    )
    items = client.get("/api/run-health/attention", headers=_auth(org)).json()["items"]
    assert not any(item["id"] == "auth:salesforce" for item in items)


def test_growing_embedding_backlog_links_to_content_panel(client, monkeypatch):
    from app.retrieval import store

    org = _owner_org("rh_attention_backlog")
    monkeypatch.setattr(
        store,
        "pending_embedding_backlog",
        lambda scoped_org: {
            "count": 75,
            "oldest_created_at": _now_iso(-3600),
            "newest_created_at": _now_iso(-1800),
        } if scoped_org == org else {"count": 0},
    )

    items = client.get("/api/run-health/attention", headers=_auth(org)).json()["items"]
    backlog = next(item for item in items if item["condition"] == "growing_embedding_backlog")
    assert backlog["severity"] == "medium"
    assert backlog["panel"] == "content"
    assert backlog["href"] == "/run-health?panel=content"
    assert backlog["details"]["pending_embeddings"] == 75


def test_repeated_stage_failures_promote_to_attention(client):
    org = _owner_org("rh_attention_stage")
    _seed_run(org, status="complete", errors={"roadmap": "first failure"})
    latest_run = _seed_run(org, status="complete", errors={"roadmap": "second failure"})

    items = client.get("/api/run-health/attention", headers=_auth(org)).json()["items"]
    repeated = next(item for item in items if item["condition"] == "repeated_stage_failure")
    assert repeated["id"] == "stage:roadmap"
    assert repeated["severity"] == "medium"
    assert repeated["run_id"] == latest_run
    assert repeated["panel"] == "runs"
    assert f"run={latest_run}" in repeated["href"]
    assert repeated["details"]["failure_count"] == 2


def test_single_stage_failure_stays_degraded_without_attention_promotion(client):
    org = _owner_org("rh_attention_transient")
    _seed_run(org, status="complete", errors={"roadmap": "transient failure"})

    runs = client.get("/api/run-health/runs", headers=_auth(org)).json()["runs"]
    items = client.get("/api/run-health/attention", headers=_auth(org)).json()["items"]
    assert runs[0]["health_status"] == "degraded"
    assert not any(item["condition"] == "repeated_stage_failure" for item in items)


# ── AC5: org-scoping — no cross-tenant visibility ───────────────────────────────

def test_no_cross_tenant_visibility(client):
    org_a = _owner_org("rh_a")
    org_b = _owner_org("rh_b")

    # Seed run-health data under org A only.
    _seed_checkpoint(org_a, "servicenow", "a-cursor", _now_iso(-60))
    _seed_run(org_a, status="complete", pack_id="ncino", opps=2)
    _seed_expired_token(org_a, "salesforce")

    # Org B sees NONE of it.
    conns_b = client.get("/api/run-health/connectors", headers=_auth(org_b)).json()
    assert conns_b["connectors"] == []

    runs_b = client.get("/api/run-health/runs", headers=_auth(org_b)).json()
    assert runs_b["runs"] == []

    packs_b = client.get("/api/run-health/packs", headers=_auth(org_b)).json()
    assert packs_b == {"run_id": None, "packs": []}

    attention_b = client.get("/api/run-health/attention", headers=_auth(org_b)).json()
    assert attention_b["items"] == []

    # Org A still sees its own.
    runs_a = client.get("/api/run-health/runs", headers=_auth(org_a)).json()
    assert len(runs_a["runs"]) == 1
    attention_a = client.get("/api/run-health/attention", headers=_auth(org_a)).json()
    assert any(item["id"] == "auth:salesforce" for item in attention_a["items"])


# ── AC6: RBAC — Analyst read-only, Viewer forbidden, unauth 401 ─────────────────

_ENDPOINTS = [
    "/api/run-health/connectors",
    "/api/run-health/runs",
    "/api/run-health/packs",
    "/api/run-health/attention",
]


@pytest.mark.parametrize("path", _ENDPOINTS)
def test_unauthenticated_is_401(client, path):
    resp = client.get(path)  # no Authorization header
    assert resp.status_code == 401


@pytest.mark.parametrize("path", _ENDPOINTS)
def test_viewer_is_forbidden(client, path):
    org = f"rh_view_{uuid4().hex[:8]}"
    headers = _set_role(org, "viewer")
    resp = client.get(path, headers=headers)
    assert resp.status_code == 403


@pytest.mark.parametrize("path", _ENDPOINTS)
def test_analyst_read_only_allowed(client, path):
    org = f"rh_anl_{uuid4().hex[:8]}"
    headers = _set_role(org, "analyst")
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200
