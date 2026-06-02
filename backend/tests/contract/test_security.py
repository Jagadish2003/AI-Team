# ===========================================================================
# T1-S11 Task 1 — Section 4: Four new security tests (AC7, AC8, AC9, AC10)
# Append these to the bottom of test_connector_auth.py
# ===========================================================================


# ---------------------------------------------------------------------------
# AC8: Open redirect ignored — redirect_to in state never affects redirect target
# ---------------------------------------------------------------------------


def test_open_redirect_ignored(client):
    """State containing redirect_to field is ignored — redirect always goes to OAUTH_SUCCESS_REDIRECT (AC8).

    Simulates an attacker embedding redirect_to=https://evil.com inside a valid
    nonce payload. The callback must redirect to the hardcoded constant only.
    """
    import json as _json
    from datetime import timedelta, timezone
    from app import db as _db
    from app.auth.vault import _NONCE_TTL_MINUTES  # noqa: WPS437

    # Build a valid nonce with redirect_to injected into the stored payload
    nonce = _secrets_mod.token_hex(16)
    key = f"nonce:{nonce}"
    now = datetime.now(timezone.utc)
    data = _json.dumps({
        "connector_id": "salesforce",
        "created_at":   now.isoformat(),
        "expires_at":   (now + timedelta(minutes=_NONCE_TTL_MINUTES)).isoformat(),
        "redirect_to":  "https://evil.com",  # attacker-injected field
    })
    con = _db.connect()
    try:
        con.execute("INSERT OR REPLACE INTO nonces (key, data) VALUES (?, ?)", (key, data))
        con.commit()
    finally:
        con.close()

    fake_token = {"access_token": "t", "expires_in": 3600}
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=valid_code&state={nonce}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303), (
        f"Expected redirect, got {resp.status_code}"
    )
    location = resp.headers["location"]
    assert "evil.com" not in location, (
        f"Open redirect not protected — location contains evil.com: {location}"
    )
    assert "connected=salesforce" in location or "/integration-hub" in location, (
        f"Redirect did not go to OAUTH_SUCCESS_REDIRECT — got: {location}"
    )


# ---------------------------------------------------------------------------
# AC9: Timing-safe state comparison — hmac.compare_digest used, not ==
# ---------------------------------------------------------------------------


def test_timing_safe_state_comparison(client):
    """One-character state mismatch returns 400; hmac.compare_digest is used (not ==) (AC9).

    Issues a valid nonce then flips one character before sending the callback.
    Also verifies via source inspection that compare_digest is present in the
    route handler — not the == operator.
    """
    import inspect as _inspect
    import app.routes_connector_auth as _rca

    # Generate a real nonce via auth-url
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert r.status_code == 200
    nonce = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    # Flip the last character → one-character mismatch
    bad_state = nonce[:-1] + ("a" if nonce[-1] != "a" else "b")

    resp = client.get(
        f"/api/connectors/oauth/callback?code=code&state={bad_state}",
        headers=_AUTH_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 400, (
        f"Expected 400 for one-character state mismatch, got {resp.status_code}"
    )

    # Verify compare_digest is present in route source — not ==
    source = _inspect.getsource(_rca)
    assert "compare_digest" in source, (
        "hmac.compare_digest() not found in routes_connector_auth source — "
        "timing-safe comparison is required (AC9)"
    )


# ---------------------------------------------------------------------------
# AC10: Nonce replay rejected — second callback with same nonce returns 400
# ---------------------------------------------------------------------------


def test_nonce_replay_rejected(client):
    """Two sequential callbacks with the same valid nonce: first succeeds (302), second returns 400 (AC10).

    Confirms the delete-before-process pattern in consume_nonce() / _consume_nonce()
    removes the nonce after first use regardless of callback outcome.
    """
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "tok", "expires_in": 3600}

    # First use — must succeed
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        r1 = client.get(
            f"/api/connectors/oauth/callback?code=code1&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )
    assert r1.status_code in (302, 303), (
        f"First callback did not succeed — got {r1.status_code}"
    )

    # Second use — same state, must fail with 400
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        r2 = client.get(
            f"/api/connectors/oauth/callback?code=code2&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )
    assert r2.status_code == 400, (
        f"Nonce replay was not rejected — expected 400, got {r2.status_code}"
    )
    _clear_credentials()


# ---------------------------------------------------------------------------
# AC7: Nonce expiry rejected — nonce older than 10 minutes returns 400
# ---------------------------------------------------------------------------


def test_nonce_expiry_rejected(client):
    """Nonce issued more than 10 minutes ago returns 400 even if never used (AC7).

    Prevents an attacker from capturing a valid nonce and replaying it later.
    Inserts a nonce directly with a backdated expires_at to simulate expiry.
    """
    import json as _json
    from datetime import timedelta, timezone
    from app import db as _db

    nonce = _secrets_mod.token_hex(16)
    key = f"nonce:{nonce}"
    now = datetime.now(timezone.utc)

    # expires_at = 11 minutes ago (created 21 min ago + 10 min TTL = expired 11 min ago)
    data = _json.dumps({
        "connector_id": "salesforce",
        "created_at":   (now - timedelta(minutes=21)).isoformat(),
        "expires_at":   (now - timedelta(minutes=11)).isoformat(),
    })
    con = _db.connect()
    try:
        con.execute("INSERT OR REPLACE INTO nonces (key, data) VALUES (?, ?)", (key, data))
        con.commit()
    finally:
        con.close()

    resp = client.get(
        f"/api/connectors/oauth/callback?code=valid_code&state={nonce}",
        headers=_AUTH_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 400, (
        f"Expired nonce was not rejected — expected 400, got {resp.status_code}"
    )