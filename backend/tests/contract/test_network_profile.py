"""R18-A3 T5 (AT-558) — NETWORK_PROFILE deployment flag + auth-capability API.

T5 adds the deployment posture flag

    NETWORK_PROFILE = 'standard' | 'no_public_inbound'

and the GET /api/network-profile endpoint that pairs it with a per-connector auth
capability map so the UI can hide the authorization-code connect flow wherever an
outbound-only mode exists (AC4) — the customer can never start a flow that cannot
complete in a no-public-inbound deployment.

These cover:
  * the flag reader (default, valid values, blank/unknown → safe default)
  * the auth_modes capability helpers (outbound-only subset, capability shape)
  * the endpoint (profile echoed, per-connector capability, auth required)
"""
from __future__ import annotations

import os
from unittest.mock import patch

from app import network_profile
from app.auth import auth_modes
from app.auth.auth_modes import (
    AUTH_MODE_AUTHORIZATION_CODE,
    AUTH_MODE_CLIENT_CREDENTIALS,
    AUTH_MODE_JWT_BEARER,
    AUTH_MODE_STATIC,
    all_known_connector_ids,
    get_connector_auth_capability,
    get_outbound_only_modes,
)

_AUTH_HEADERS = {"Authorization": "Bearer dev-token-change-me"}


# ---------------------------------------------------------------------------
# The NETWORK_PROFILE flag reader
# ---------------------------------------------------------------------------


def test_default_profile_is_standard_when_unset():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NETWORK_PROFILE", None)
        assert network_profile.get_network_profile() == "standard"
        assert network_profile.is_no_public_inbound() is False


def test_no_public_inbound_recognised():
    with patch.dict(os.environ, {"NETWORK_PROFILE": "no_public_inbound"}):
        assert network_profile.get_network_profile() == "no_public_inbound"
        assert network_profile.is_no_public_inbound() is True


def test_profile_is_case_and_whitespace_tolerant():
    with patch.dict(os.environ, {"NETWORK_PROFILE": "  No_Public_Inbound  "}):
        assert network_profile.get_network_profile() == "no_public_inbound"


def test_unknown_or_blank_profile_falls_back_to_standard():
    # An unknown/blank value must fail toward the FULL experience, never silently
    # hide connect flows.
    for bad in ("", "   ", "bogus", "no_inbound"):
        with patch.dict(os.environ, {"NETWORK_PROFILE": bad}):
            assert network_profile.get_network_profile() == "standard"
            assert network_profile.is_no_public_inbound() is False


# ---------------------------------------------------------------------------
# auth_modes capability helpers
# ---------------------------------------------------------------------------


def test_outbound_only_modes_for_connectors():
    # Salesforce: jwt_bearer is the outbound-only path (authorization_code excluded).
    assert get_outbound_only_modes("salesforce") == (AUTH_MODE_JWT_BEARER,)
    # ServiceNow: client_credentials + static, in preference order.
    assert get_outbound_only_modes("servicenow") == (
        AUTH_MODE_CLIENT_CREDENTIALS,
        AUTH_MODE_STATIC,
    )
    # Jira: static only.
    assert get_outbound_only_modes("jira") == (AUTH_MODE_STATIC,)
    # Teams / SharePoint: client_credentials only.
    assert get_outbound_only_modes("teams") == (AUTH_MODE_CLIENT_CREDENTIALS,)
    assert get_outbound_only_modes("sharepoint") == (AUTH_MODE_CLIENT_CREDENTIALS,)
    # GitHub / Slack: NO outbound-only mode (authorization_code only).
    assert get_outbound_only_modes("github") == ()
    assert get_outbound_only_modes("slack") == ()


def test_capability_shape_for_salesforce():
    cap = get_connector_auth_capability("salesforce")
    assert cap["connector_id"] == "salesforce"
    assert cap["supported_auth_modes"] == [
        AUTH_MODE_AUTHORIZATION_CODE,
        AUTH_MODE_JWT_BEARER,
    ]
    assert cap["outbound_only_modes"] == [AUTH_MODE_JWT_BEARER]
    assert cap["has_outbound_only_mode"] is True
    assert cap["default_auth_mode"] == AUTH_MODE_AUTHORIZATION_CODE


def test_capability_for_authorization_code_only_connector():
    # GitHub has no outbound-only mode — the UI must NOT hide its Connect button
    # (it falls back to the scoped-inbound package instead, R18-A3 T6).
    cap = get_connector_auth_capability("github")
    assert cap["has_outbound_only_mode"] is False
    assert cap["outbound_only_modes"] == []
    assert cap["default_auth_mode"] == AUTH_MODE_AUTHORIZATION_CODE


def test_capability_for_unknown_connector_is_empty_not_raising():
    cap = get_connector_auth_capability("does_not_exist")
    assert cap["supported_auth_modes"] == []
    assert cap["has_outbound_only_mode"] is False
    assert cap["default_auth_mode"] is None


def test_all_known_connector_ids_includes_oauth_and_static_only():
    ids = all_known_connector_ids()
    for expected in ("salesforce", "servicenow", "jira", "teams", "postgresql", "oracle_db"):
        assert expected in ids


# ---------------------------------------------------------------------------
# GET /api/network-profile endpoint
# ---------------------------------------------------------------------------


def test_endpoint_requires_auth(client):
    resp = client.get("/api/network-profile")
    assert resp.status_code in (401, 403)


def test_endpoint_returns_standard_by_default(client):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NETWORK_PROFILE", None)
        resp = client.get("/api/network-profile", headers=_AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["network_profile"] == "standard"
    assert body["no_public_inbound"] is False
    # Every known connector is described.
    assert "salesforce" in body["connectors"]
    sf = body["connectors"]["salesforce"]
    assert sf["has_outbound_only_mode"] is True
    assert sf["outbound_only_modes"] == ["jwt_bearer"]


def test_endpoint_reports_no_public_inbound(client):
    with patch.dict(os.environ, {"NETWORK_PROFILE": "no_public_inbound"}):
        resp = client.get("/api/network-profile", headers=_AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["network_profile"] == "no_public_inbound"
    assert body["no_public_inbound"] is True
    # GitHub/Slack have no outbound-only mode → UI keeps their Connect button (AC4
    # only hides where an outbound-only mode EXISTS).
    assert body["connectors"]["github"]["has_outbound_only_mode"] is False
    assert body["connectors"]["slack"]["has_outbound_only_mode"] is False
    # Salesforce / ServiceNow / Teams DO have an outbound-only mode → UI hides
    # their authorization-code Connect button.
    assert body["connectors"]["salesforce"]["has_outbound_only_mode"] is True
    assert body["connectors"]["servicenow"]["has_outbound_only_mode"] is True
    assert body["connectors"]["teams"]["has_outbound_only_mode"] is True
