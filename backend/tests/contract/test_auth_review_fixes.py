"""Regression tests for the AUTH-1 security review fixes.

Covers the review findings that need route-, middleware-, or migration-level
exercise rather than the pure logic-layer assertions in test_user_auth.py:

  #15 — security._jwt_role fails closed on a forged-signature token (no
        privilege escalation from an unverified role claim).
  #3  — the tenancy middleware trusts org_id only from a signature-verified
        token; a forged token contributes no org claim.
  #13 — change-password invalidates a previously-issued JWT end-to-end through
        the real /api/auth/* routes (regression for #4).
  #16 — org isolation: GET /api/runs returns only the caller-org's runs, even
        for two separately-registered orgs (regression for #5 + #10).
  #17 — the 0001→0006 Alembic chain upgrades to head and downgrades to base
        cleanly (regression for the #6 renumbering).
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid
from pathlib import Path

import jwt
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from app import db, security
from app.auth import user_auth
from app.middleware import tenancy

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


# ---------------------------------------------------------------------------
# #15 — _jwt_role fails closed on an unverified / forged token
# ---------------------------------------------------------------------------


def test_jwt_role_rejects_forged_signature_token():
    """A token with a forged signature and an inflated role claim yields None.

    Before the fix _jwt_role decoded with verify_signature=False, so any caller
    that used the role for an authz decision without an upstream require_auth
    would trust the forged 'owner' claim (privilege escalation, review #1/#15).
    """
    forged = jwt.encode(
        {"role": "owner", "sub": "attacker", "exp": int(time.time()) + 600},
        "not-the-real-secret",
        algorithm="HS256",
    )
    assert security._jwt_role(forged) is None


def test_jwt_role_maps_owner_to_admin_for_valid_token():
    good = user_auth.issue_jwt("u1", "org1", "owner", "owner@example.com")
    assert security._jwt_role(good) == "admin"
    analyst = user_auth.issue_jwt("u2", "org1", "analyst", "a@example.com")
    assert security._jwt_role(analyst) == "analyst"


def test_jwt_role_rejects_revoked_token():
    """A logged-out token's role claim is no longer trusted (fail closed)."""
    token = user_auth.issue_jwt("u3", "org1", "owner", "o@example.com")
    user_auth.logout_token(token)
    assert security._jwt_role(token) is None


# ---------------------------------------------------------------------------
# #3 — tenancy trusts org_id only from a signature-verified token
# ---------------------------------------------------------------------------


def test_tenancy_ignores_forged_org_claim():
    forged = jwt.encode(
        {
            "org_id": "victim-org",
            "sub": "attacker",
            "jti": str(uuid.uuid4()),
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        "not-the-real-secret",
        algorithm="HS256",
    )
    # Forged signature → no verified payload → its org_id is never trusted.
    assert tenancy._verified_jwt_payload(forged) is None


def test_tenancy_trusts_signed_org_claim():
    good = user_auth.issue_jwt("u9", "real-org", "owner", "o@example.com")
    payload = tenancy._verified_jwt_payload(good)
    assert payload is not None
    assert payload["org_id"] == "real-org"


# ---------------------------------------------------------------------------
# #13 — change-password invalidates an existing JWT (end-to-end via routes)
# ---------------------------------------------------------------------------


def test_change_password_invalidates_existing_token_end_to_end(client):
    email = _email()
    reg = client.post(
        "/api/auth/register",
        json={"org_name": f"Org {uuid.uuid4().hex[:6]}", "email": email, "password": "supersecret1"},
    )
    assert reg.status_code == 201, reg.text
    token = reg.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Token works before the change.
    assert client.get("/api/auth/me", headers=auth).status_code == 200

    # Rotate the password using that same token.
    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "supersecret1", "new_password": "evenbetter2"},
        headers=auth,
    )
    assert changed.status_code == 204, changed.text

    # The old token is now revoked on every protected route.
    assert client.get("/api/auth/me", headers=auth).status_code == 401


# ---------------------------------------------------------------------------
# #16 / #10 — GET /api/runs is strictly org-scoped
# ---------------------------------------------------------------------------


def test_runs_list_is_org_scoped_between_two_orgs(client):
    org_a = user_auth.register_org_and_owner(f"A {uuid.uuid4().hex[:6]}", _email(), "supersecret1")
    org_b = user_auth.register_org_and_owner(f"B {uuid.uuid4().hex[:6]}", _email(), "supersecret2")
    org_a_id, org_b_id = org_a["user"]["org_id"], org_b["user"]["org_id"]

    run_a = f"RUN_A_{uuid.uuid4().hex[:8]}"
    run_b = f"RUN_B_{uuid.uuid4().hex[:8]}"
    db.upsert("runs", run_a, {"id": run_a, "org_id": org_a_id, "status": "complete", "startedAt": "2026-01-01T00:00:00Z"})
    db.upsert("runs", run_b, {"id": run_b, "org_id": org_b_id, "status": "complete", "startedAt": "2026-01-02T00:00:00Z"})

    a_resp = client.get("/api/runs", headers={"Authorization": f"Bearer {org_a['token']}"})
    b_resp = client.get("/api/runs", headers={"Authorization": f"Bearer {org_b['token']}"})
    assert a_resp.status_code == 200 and b_resp.status_code == 200

    a_ids = {r["id"] for r in a_resp.json()}
    b_ids = {r["id"] for r in b_resp.json()}
    assert run_a in a_ids and run_b not in a_ids
    assert run_b in b_ids and run_a not in b_ids


# ---------------------------------------------------------------------------
# #17 / #6 — Alembic chain upgrades to head and downgrades to base cleanly
# ---------------------------------------------------------------------------


def test_alembic_chain_upgrade_then_downgrade_is_clean():
    """The 0001→0007 Alembic chain upgrades to head and downgrades to base
    cleanly. AT-288 / Fix 1: runs in an isolated throwaway PostgreSQL schema
    (via libpq PGOPTIONS search_path) so the destructive `downgrade base` never
    touches the shared test database the rest of the suite depends on.
    """
    import psycopg2

    base_url = os.environ["DATABASE_URL"]
    schema = f"alembic_chain_{uuid.uuid4().hex[:8]}"

    admin = psycopg2.connect(base_url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()

    old_pgoptions = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = f"-c search_path={schema}"
    try:
        cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        # Skip env.py's fileConfig() so it doesn't disable active loggers mid-suite.
        cfg.config_file_name = None
        # A broken down_revision chain raises MultipleHeads / KeyError here.
        alembic_command.upgrade(cfg, "head")
        alembic_command.downgrade(cfg, "base")
    finally:
        if old_pgoptions is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = old_pgoptions
        admin = psycopg2.connect(base_url)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()