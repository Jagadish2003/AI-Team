"""Proactive OAuth token-refresher background job.

The job renews vault tokens BEFORE they expire so connected sources stay live
without the user re-running the OAuth flow. These tests cover the due-token query
(only refreshable rows within the lookahead), the per-connector isolation of the
run loop, and the get_token lookahead that performs the actual proactive refresh.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

import app.jobs.token_refresher as tr
from app import db
from app.auth.models import ConnectorAuthConfig
from app.auth.vault import get_token, store_token

_VAULT_KEY = None


def _vault_env() -> dict:
    global _VAULT_KEY
    if _VAULT_KEY is None:
        _VAULT_KEY = Fernet.generate_key().decode()
    os.environ["CREDENTIAL_VAULT_KEY"] = _VAULT_KEY
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY}


def _clear(org_prefix: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE credentials SET is_deleted = TRUE WHERE org_id LIKE %s",
            (f"{org_prefix}%",),
        )
        con.commit()
    finally:
        con.close()


def _store(org, conn, *, expires_in=None, refresh_token="r"):
    resp = {"access_token": "a"}
    if refresh_token is not None:
        resp["refresh_token"] = refresh_token
    if expires_in is not None:
        resp["expires_in"] = expires_in
    store_token(org, conn, resp)


def _set_refresh_failed(org, conn):
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE credentials SET refresh_failed = 1 WHERE org_id = %s AND connector_id = %s",
            (org, conn),
        )
        con.commit()
    finally:
        con.close()


# ── get_refreshable_credentials ──────────────────────────────────────────────
def test_get_refreshable_returns_only_due_refreshable_rows():
    org = "org-tr-due"
    _clear(org)
    with patch.dict(os.environ, _vault_env()):
        _store(org, "salesforce", expires_in=60, refresh_token="r1")      # due + refreshable
        _store(org, "jira", expires_in=7200, refresh_token="r2")          # not due (far future)
        _store(org, "github", expires_in=60, refresh_token=None)          # due but no refresh token
        _store(org, "servicenow", expires_in=60, refresh_token="r3")      # due, will be refresh_failed
        _set_refresh_failed(org, "servicenow")

        due = {(o, c) for o, c in tr.get_refreshable_credentials(900) if o == org}

    assert (org, "salesforce") in due
    assert (org, "jira") not in due       # outside the lookahead window
    assert (org, "github") not in due     # no refresh token → not refreshable
    assert (org, "servicenow") not in due # refresh_failed → leave for reconnect
    _clear(org)


# ── run_token_refresh_job ────────────────────────────────────────────────────
def test_run_job_refreshes_each_due_and_isolates_failures(monkeypatch):
    monkeypatch.setattr(
        tr, "get_refreshable_credentials", lambda ahead: [("o", "salesforce"), ("o", "jira")]
    )
    calls = []

    async def fake_get_token(org_id, connector_id, *, min_validity_seconds):
        calls.append((org_id, connector_id, min_validity_seconds))
        if connector_id == "jira":
            raise tr.ConnectorNotAuthenticatedError(org_id, connector_id)
        return object()

    monkeypatch.setattr(tr, "get_token", fake_get_token)

    # Must not raise even though one connector fails to refresh.
    tr.run_token_refresh_job()

    assert ("o", "salesforce", tr.TOKEN_REFRESH_AHEAD_SECONDS) in calls
    assert ("o", "jira", tr.TOKEN_REFRESH_AHEAD_SECONDS) in calls


def test_run_job_is_noop_when_nothing_due(monkeypatch):
    called = {"n": 0}

    async def fake_get_token(*a, **k):
        called["n"] += 1
        return object()

    monkeypatch.setattr(tr, "get_refreshable_credentials", lambda ahead: [])
    monkeypatch.setattr(tr, "get_token", fake_get_token)

    tr.run_token_refresh_job()
    assert called["n"] == 0


# ── get_token lookahead (the proactive refresh the job relies on) ─────────────
def test_get_token_lookahead_refreshes_token_with_plenty_of_life():
    """With a widened min_validity_seconds, get_token refreshes a token that is
    NOT near expiry — this is what lets the job renew tokens ahead of time."""
    org = "org-tr-look"
    _clear(org)
    cfg = ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://example.com/token",
        scopes=["api"],
    )
    new_resp = {"access_token": "refreshed-ahead", "refresh_token": "keep", "expires_in": 7200}

    with patch.dict(os.environ, {**_vault_env(), "_TEST_OAUTH_SECRET": "s"}):
        _store(org, "salesforce", expires_in=7200, refresh_token="orig")  # ~2h left
        with patch(
            "app.auth.vault._oauth.refresh_token",
            new_callable=AsyncMock,
            return_value=new_resp,
        ) as mock_refresh, patch(
            "app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"salesforce": cfg}
        ):
            # Default threshold (300s) would NOT refresh a 2h token …
            rec_default = asyncio.run(get_token(org, "salesforce"))
            assert rec_default.access_token == "a"
            mock_refresh.assert_not_called()

            # … but a 3h lookahead does.
            rec_ahead = asyncio.run(
                get_token(org, "salesforce", min_validity_seconds=10800)
            )
            assert rec_ahead.access_token == "refreshed-ahead"
            mock_refresh.assert_called_once()
    _clear(org)
