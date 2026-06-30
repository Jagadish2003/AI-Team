"""Tenant Isolation Hardening — full contract suite (R17-D3 / AT-451, T6).

This is the consolidated contract test suite for R17-D3 Section 5. It exercises
every acceptance criterion of the story through the public route / contract
surface, so the isolation guarantee is proven end-to-end rather than only at the
unit level (the per-task suites — ``test_connector_auth.py``,
``test_cross_tenant_isolation.py``, ``test_event_org_attribution.py`` — cover the
pieces; this file proves the whole story holds together).

Jira AC → R17-D3 AC mapping:

  * T6-AC1 (AC1) — connector OAuth store / read / revoke are scoped to the
    authenticated org; the hardcoded 'default' org is gone from the OAuth path.
  * T6-AC2 (AC2) — the OAuth callback verifies the org carried in the signed
    ``state`` parameter and cannot be bound to a different tenant than initiated.
  * T6-AC3 (AC3–AC5) — cross-tenant leakage is prevented across connectors,
    discovery findings, ingestion checkpoints, evidence, and the graph.
  * T6-AC4 (AC4) — two orgs connecting the SAME connector type do not collide.
  * T6-AC5 (AC6) — telemetry and audit events are attributed to the correct org.
  * T6-AC6 (AC7) — the isolation half of the definition of done: multiple
    customers running isolated, authenticated, audited discovery.

Auth model in tests: the static dev token carries no signed org claim, so the
tenancy middleware resolves the request org from the ``X-Org-Id`` header. Sending
``X-Org-Id`` therefore authenticates the request as that org. Role-gated routes
(opportunities/evidence = viewer+, graph = analyst+) also need a membership row,
seeded via ``seed_owner(org, dev_token)`` so the test exercises the TENANCY guard
(404), not the RBAC gate (403).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os as _os
import sqlite3
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from database.models.entities import Entity
from database.models.entity_relationships import EntityRelationship

_DEV_TOKEN = "dev-token-change-me"

# One Fernet key for the whole module so vault store/read round-trips.
_VAULT_KEY = Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _org(prefix: str) -> str:
    """A fresh, unique org id (so a session-shared DB never bleeds state in)."""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _auth(org_id: str) -> dict:
    """Headers that authenticate the dev user as ``org_id`` (X-Org-Id drives the
    org because the dev token carries no signed org claim)."""
    return {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org_id}


def _vault_env() -> dict:
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY}


def _token_response(access_token: str, *, expires_in: int = 7200) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": "refresh-" + access_token,
        "expires_in": expires_in,
        "scope": "read",
    }


@contextlib.contextmanager
def _patch_env(values: dict):
    saved = {k: _os.environ.get(k) for k in values}
    _os.environ.update(values)
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = old


@contextlib.contextmanager
def _org_context(org_id: str | None):
    """Set the tenancy ContextVar for the block (mirrors TenancyMiddleware per
    request), then restore it — for attribution tests that run outside a request."""
    from app.middleware import tenancy

    token = tenancy._current_org_id.set(org_id)
    try:
        yield
    finally:
        tenancy._current_org_id.reset(token)


def _state_from_auth_url(client: TestClient, org_id: str, connector: str = "salesforce") -> str:
    """Initiate an OAuth flow as ``org_id`` and return the signed ``state`` the
    provider would echo back (carries the initiating org + single-use nonce)."""
    r = client.get(f"/api/connectors/{connector}/auth-url", headers=_auth(org_id))
    assert r.status_code == 200, r.text
    return parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]


# Graph seeding helpers (mirror test_cross_tenant_isolation.py). Native PostgreSQL
# SQL; sqlite3.connect is routed to the test pool by conftest.


def _insert_entity(org_id: str, display_name: str, *, run_id: str) -> str:
    entity = Entity(
        org_id=org_id,
        entity_type="person",
        canonical_name=" ".join(display_name.split()).lower() + "-" + uuid.uuid4().hex[:8],
        display_name=display_name,
        source_system="test",
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )
    row = entity.to_db_row()
    with sqlite3.connect(_os.environ.get("DB_PATH", "")) as conn:
        conn.execute(
            """INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name,
                source_system, source_record_id, resolution_confidence,
                resolution_status, first_seen_run_id, last_seen_run_id,
                run_count, metadata, created_at, updated_at
            ) VALUES (
                %(id)s, %(org_id)s, %(entity_type)s, %(canonical_name)s, %(display_name)s,
                %(source_system)s, %(source_record_id)s, %(resolution_confidence)s,
                %(resolution_status)s, %(first_seen_run_id)s, %(last_seen_run_id)s,
                %(run_count)s, %(metadata)s, %(created_at)s, %(updated_at)s
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _insert_relationship(org_id: str, from_id: str, to_id: str, *, run_id: str) -> str:
    rel = EntityRelationship(
        org_id=org_id,
        from_entity_id=from_id,
        to_entity_id=to_id,
        relationship_type="owns",
        confidence=0.9,
        inferred=False,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )
    row = rel.to_db_row()
    with sqlite3.connect(_os.environ.get("DB_PATH", "")) as conn:
        conn.execute(
            """INSERT INTO entity_relationships (
                id, org_id, from_entity_id, to_entity_id, relationship_type,
                confidence, inferred, evidence, first_seen_run_id,
                last_seen_run_id, run_count, created_at
            ) VALUES (
                %(id)s, %(org_id)s, %(from_entity_id)s, %(to_entity_id)s, %(relationship_type)s,
                %(confidence)s, %(inferred)s, %(evidence)s, %(first_seen_run_id)s,
                %(last_seen_run_id)s, %(run_count)s, %(created_at)s
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _audit_rows_for_org(org_id: str) -> list[dict]:
    from app import db

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT org_id, event_type, connector_id FROM audit_log WHERE org_id = %s",
            (org_id,),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [{"org_id": r[0], "event_type": r[1], "connector_id": r[2]} for r in rows]


# ===========================================================================
# T6-AC1 (AC1) — connector OAuth store / read / revoke scoped to authenticated org
# ===========================================================================


def test_auth_url_binds_state_to_the_authenticated_org(client: TestClient):
    """The OAuth initiation route captures the AUTHENTICATED org into the signed
    state — never a hardcoded 'default'. Two different callers get two different
    bound orgs."""
    from app.auth.oauth_state import decode_state

    org_a, org_b = _org("ac1_a"), _org("ac1_b")

    state_a = _state_from_auth_url(client, org_a)
    state_b = _state_from_auth_url(client, org_b)

    assert decode_state(state_a)["org_id"] == org_a
    assert decode_state(state_b)["org_id"] == org_b
    # The defining bug this story fixes: the bound org is the real tenant, not 'default'.
    assert decode_state(state_a)["org_id"] != "default"


def test_token_store_and_status_are_org_scoped(client: TestClient):
    """A token stored for org A reads back as connected for A and needs_auth for B."""
    from app.auth.vault import store_token

    org_a, org_b = _org("ac1s_a"), _org("ac1s_b")
    connector = "salesforce"

    with _patch_env(_vault_env()):
        store_token(org_a, connector, _token_response("orgA-token"))

    own = client.get(f"/api/connectors/{connector}/token-status", headers=_auth(org_a))
    assert own.status_code == 200 and own.json()["status"] == "connected"

    other = client.get(f"/api/connectors/{connector}/token-status", headers=_auth(org_b))
    assert other.status_code == 200
    assert other.json()["status"] == "needs_auth", "org B must not see org A's token"


def test_token_revoke_is_org_scoped(client: TestClient):
    """Revoking via DELETE as org B must not revoke org A's token for the same
    connector — store/revoke are strictly per org."""
    from app.auth.vault import store_token

    org_a, org_b = _org("ac1r_a"), _org("ac1r_b")
    connector = "jira"

    with _patch_env(_vault_env()):
        store_token(org_a, connector, _token_response("orgA-jira"))
        store_token(org_b, connector, _token_response("orgB-jira"))

        # Org B revokes its own token.
        revoked = client.delete(
            f"/api/connectors/{connector}/token", headers=_auth(org_b)
        )
        assert revoked.status_code == 204

        # Org A's token is untouched; org B's is gone.
        a_status = client.get(
            f"/api/connectors/{connector}/token-status", headers=_auth(org_a)
        ).json()["status"]
        b_status = client.get(
            f"/api/connectors/{connector}/token-status", headers=_auth(org_b)
        ).json()["status"]

    assert a_status == "connected", "org A's token must survive org B's revoke"
    assert b_status == "needs_auth", "org B's own token must be revoked"


# ===========================================================================
# T6-AC2 (AC2) — OAuth callback verifies org via the signed state parameter
# ===========================================================================


def test_oauth_callback_completes_for_the_initiating_org(client: TestClient):
    """Happy path: the org that initiated the flow gets the token stored under it.

    The callback derives the org from the signed state + server-side nonce, not
    from request input, so the credential lands under the initiating tenant."""
    org_a = _org("ac2ok_a")
    connector = "salesforce"
    state = _state_from_auth_url(client, org_a, connector)

    fake_token = _token_response("callback-issued-token")
    with _patch_env(_vault_env()), patch(
        "app.routes_connector_auth.exchange_code",
        new=AsyncMock(return_value=fake_token),
    ):
        resp = client.get(
            "/api/connectors/oauth/callback",
            params={"code": "auth-code-xyz", "state": state},
            headers=_auth(org_a),
            follow_redirects=False,
        )
        # Provider→backend→frontend success redirect.
        assert resp.status_code == 302
        assert "status=success" in resp.headers["location"]

        # The token is stored under the initiating org → it reads as connected.
        status = client.get(
            f"/api/connectors/{connector}/token-status", headers=_auth(org_a)
        ).json()["status"]
    assert status == "connected"


def test_oauth_callback_rejects_a_tampered_org_in_state(client: TestClient):
    """AC2: swapping the org_id segment of the signed state invalidates the HMAC,
    so the callback cannot be bound to a different tenant — generic 400."""
    org_a, org_victim = _org("ac2t_a"), _org("ac2t_victim")
    state = _state_from_auth_url(client, org_a)

    # state = "<org_id>.<nonce>.<signature>"; re-point org_id, keep nonce+sig.
    _orig_org, nonce, signature = state.rsplit(".", 2)
    tampered = f"{org_victim}.{nonce}.{signature}"

    with _patch_env(_vault_env()), patch(
        "app.routes_connector_auth.exchange_code", new=AsyncMock()
    ) as exch:
        resp = client.get(
            "/api/connectors/oauth/callback",
            params={"code": "auth-code-xyz", "state": tampered},
            headers=_auth(org_a),
            follow_redirects=False,
        )
    assert resp.status_code == 400
    exch.assert_not_called()  # rejected before any token exchange


def test_oauth_callback_rejects_a_replayed_state(client: TestClient):
    """AC2: the state nonce is single-use — replaying a state already consumed by a
    successful callback is rejected (400), so a captured callback cannot be reused."""
    org_a = _org("ac2replay_a")
    state = _state_from_auth_url(client, org_a)

    with _patch_env(_vault_env()), patch(
        "app.routes_connector_auth.exchange_code",
        new=AsyncMock(return_value=_token_response("replay-token")),
    ):
        first = client.get(
            "/api/connectors/oauth/callback",
            params={"code": "code-1", "state": state},
            headers=_auth(org_a),
            follow_redirects=False,
        )
        assert first.status_code == 302  # consumed the nonce

        second = client.get(
            "/api/connectors/oauth/callback",
            params={"code": "code-2", "state": state},
            headers=_auth(org_a),
            follow_redirects=False,
        )
    assert second.status_code == 400, "a replayed (already-consumed) state must be rejected"


def test_oauth_callback_rejects_a_malformed_state(client: TestClient):
    """AC2: an unsigned / garbage state never passes signature verification → 400."""
    org_a = _org("ac2bad_a")
    with patch("app.routes_connector_auth.exchange_code", new=AsyncMock()) as exch:
        resp = client.get(
            "/api/connectors/oauth/callback",
            params={"code": "code", "state": "not-a-signed-state"},
            headers=_auth(org_a),
            follow_redirects=False,
        )
    assert resp.status_code == 400
    exch.assert_not_called()


# ===========================================================================
# T6-AC3 (AC3–AC5) — cross-tenant leakage prevented across every surface
# ===========================================================================


def test_no_cross_tenant_read_across_every_surface(client: TestClient):
    """Authenticated as org B, NO path returns org A's connectors, findings,
    checkpoints, evidence, or graph (R17-D3 Section 3 / AC3–AC5)."""
    from app import db
    from app.auth.vault import get_token, store_token
    from app.rbac import seed_owner
    from discovery.ingest.base import Checkpoint
    from discovery.ingest.checkpoint_repository import read_checkpoint, save_checkpoint

    org_a, org_b = _org("ac3_a"), _org("ac3_b")
    seed_owner(org_a, _DEV_TOKEN)
    seed_owner(org_b, _DEV_TOKEN)

    # --- seed org A across every tenant-sensitive surface ---
    with _patch_env(_vault_env()):
        store_token(org_a, "salesforce", _token_response("A-sf-token"))

    run_id = "iso_run_" + uuid.uuid4().hex[:10]
    db.upsert_run(run_id, {"id": run_id, "org_id": org_a, "status": "done"})
    db.run_kv_set("opps", run_id, [{"id": "opp-A", "title": "A finding", "impact": 5}])
    db.run_kv_set(
        "evidence", run_id, [{"id": "ev-A", "summary": "A evidence", "decision": "UNREVIEWED"}]
    )

    save_checkpoint(
        Checkpoint(
            connector_id="salesforce",
            org_id=org_a,
            value="A-checkpoint",
            captured_at="2026-06-29T00:00:00Z",
        )
    )

    graph_run = "iso_graph_" + uuid.uuid4().hex[:8]
    e1 = _insert_entity(org_a, "A Entity One", run_id=graph_run)
    e2 = _insert_entity(org_a, "A Entity Two", run_id=graph_run)
    _insert_relationship(org_a, e1, e2, run_id=graph_run)

    # --- org A positive controls: it CAN read its own data ---
    assert client.get(f"/api/runs/{run_id}/opportunities", headers=_auth(org_a)).status_code == 200
    assert client.get(f"/api/runs/{run_id}/evidence", headers=_auth(org_a)).status_code == 200

    # --- authenticated as org B: assert nothing of A's leaks ---

    # connectors — token status + vault both deny
    assert (
        client.get("/api/connectors/salesforce/token-status", headers=_auth(org_b)).json()[
            "status"
        ]
        == "needs_auth"
    )
    with _patch_env(_vault_env()):
        try:
            asyncio.run(get_token(org_b, "salesforce"))
            raise AssertionError("org B unexpectedly retrieved a connector token")
        except Exception as exc:  # ConnectorNotAuthenticatedError
            assert (
                "salesforce" in str(exc)
                or exc.__class__.__name__ == "ConnectorNotAuthenticatedError"
            )

    # discovery findings + evidence — denied as 404 (indistinguishable from not-found)
    assert client.get(f"/api/runs/{run_id}", headers=_auth(org_b)).status_code == 404
    assert (
        client.get(f"/api/runs/{run_id}/opportunities", headers=_auth(org_b)).status_code == 404
    )
    assert client.get(f"/api/runs/{run_id}/evidence", headers=_auth(org_b)).status_code == 404
    assert run_id not in [
        r.get("id") for r in client.get("/api/runs", headers=_auth(org_b)).json()
    ]

    # ingestion checkpoints — keyed by (org_id, connector_id)
    assert read_checkpoint(org_b, "salesforce") is None

    # graph — no traversal, empty summary
    assert (
        client.get(f"/api/graph/entity/{e1}/neighbourhood", headers=_auth(org_b)).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/graph/path?from_entity_id={e1}&to_entity_id={e2}", headers=_auth(org_b)
        ).status_code
        == 404
    )
    summary_b = client.get("/api/graph/org/summary", headers=_auth(org_b)).json()
    assert sum(summary_b["entity_counts_by_type"].values()) == 0
    assert sum(summary_b["relationship_counts_by_type"].values()) == 0


# ===========================================================================
# T6-AC4 (AC4) — two orgs connecting the same connector type do not collide
# ===========================================================================


def test_two_orgs_same_connector_type_do_not_collide(client: TestClient):
    """Each customer holds its own isolated token for the SAME connector — neither
    overwrites nor reads the other's, at both the vault and the route layer."""
    from app.auth.vault import get_token, store_token

    org_a, org_b = _org("ac4_a"), _org("ac4_b")
    connector = "servicenow"

    with _patch_env(_vault_env()):
        store_token(org_a, connector, _token_response("orgA-secret"))
        store_token(org_b, connector, _token_response("orgB-secret"))

        rec_a = asyncio.run(get_token(org_a, connector))
        rec_b = asyncio.run(get_token(org_b, connector))

    assert rec_a.access_token == "orgA-secret"
    assert rec_b.access_token == "orgB-secret"
    assert rec_a.access_token != rec_b.access_token, "tokens collided across tenants"

    # Route layer agrees: both independently connected.
    for org in (org_a, org_b):
        assert (
            client.get(f"/api/connectors/{connector}/token-status", headers=_auth(org)).json()[
                "status"
            ]
            == "connected"
        )


# ===========================================================================
# T6-AC5 (AC6) — telemetry and audit events attributed to the correct org
# ===========================================================================


def test_telemetry_event_is_attributed_to_the_authenticated_org():
    """Inside a request the authenticated org wins over any payload org_id, and an
    unresolved event is UNATTRIBUTED — never the real 'default' tenant."""
    from app.middleware.tenancy import UNATTRIBUTED_ORG
    from app.telemetry import record_event

    written: list = []
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.add.side_effect = written.append
    session.commit = MagicMock()

    with patch("app.telemetry.get_db_session", return_value=session):
        with _org_context("org_authn"):
            record_event("run.started", {"org_id": "org_from_payload", "source": "t"})
        with _org_context(None):
            record_event("run.started", {"source": "run_pipeline"})  # no resolvable org

    assert written[0].org_id == "org_authn", "authenticated context must win"
    assert written[1].org_id == UNATTRIBUTED_ORG
    assert written[1].org_id != "default", "must never be filed under the real 'default' tenant"


def test_audit_event_is_attributed_to_the_authenticated_org():
    """Inside a request the authenticated org is authoritative; a spoofed org_id
    argument cannot redirect the audit row to another tenant."""
    from app.middleware.audit import log_event

    ctx_org = _org("ac6_ctx")
    with _org_context(ctx_org):
        log_event("connector_connected", org_id="org_spoof", connector_id="sf")

    assert len(_audit_rows_for_org(ctx_org)) == 1
    assert _audit_rows_for_org("org_spoof") == [], "audit must not file under a spoofed org"


# ===========================================================================
# T6-AC6 (AC7) — isolation half of the definition of done:
#                isolated, authenticated, audited.
# ===========================================================================


def test_dod_isolated_authenticated_audited(client: TestClient):
    """A full authenticated OAuth connect by org A is, in one flow, AUTHENTICATED
    (Bearer + signed state), AUDITED (connector_connected filed under org A), and
    ISOLATED (org B cannot see the resulting connection) — the R17-D3 DoD."""
    from app.rbac import seed_owner

    org_a, org_b = _org("ac7_a"), _org("ac7_b")
    seed_owner(org_a, _DEV_TOKEN)
    seed_owner(org_b, _DEV_TOKEN)
    connector = "salesforce"

    state = _state_from_auth_url(client, org_a, connector)
    with _patch_env(_vault_env()), patch(
        "app.routes_connector_auth.exchange_code",
        new=AsyncMock(return_value=_token_response("dod-token")),
    ):
        resp = client.get(
            "/api/connectors/oauth/callback",
            params={"code": "dod-code", "state": state},
            headers=_auth(org_a),
            follow_redirects=False,
        )
        assert resp.status_code == 302 and "status=success" in resp.headers["location"]

        # ISOLATED + AUTHENTICATED: A sees connected, B sees needs_auth.
        a_status = client.get(
            f"/api/connectors/{connector}/token-status", headers=_auth(org_a)
        ).json()["status"]
        b_status = client.get(
            f"/api/connectors/{connector}/token-status", headers=_auth(org_b)
        ).json()["status"]
    assert a_status == "connected"
    assert b_status == "needs_auth"

    # AUDITED: the connect is recorded under org A (the authenticated tenant),
    # and never under org B.
    a_events = {e["event_type"] for e in _audit_rows_for_org(org_a)}
    assert "connector_connected" in a_events
    assert _audit_rows_for_org(org_b) == []
