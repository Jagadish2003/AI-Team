"""R18-A3 T1 (AT-554) — connector auth-mode abstraction contract tests.

T1 gives connector auth a MODE concept (authorization_code / client_credentials /
jwt_bearer / static), lets each connector register its supported modes, lets a
per-org configuration select one, and — the load-bearing invariant (AC3) — keeps
every mode terminating in the same vault record shape so downstream ingestion
resolves credentials mode-agnostically through get_connector_credentials() and
never branches on auth mode.

These cover:
  * the AuthMode type + mode constants (recognises all four; outbound-only set)
  * every connector declaring its supported modes, default == flow
  * the registry helpers (supported / default / supports)
  * the per-org selection round-trip (set / resolve / isolation / fallback)
  * AC3: get_connector_credentials resolves both record types with NO mode input,
    and no ingestion module references the mode concept.

FAKE CREDENTIALS: every credential value below is a non-real, test-only value.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth import auth_modes
from app.auth.auth_modes import (
    ALL_AUTH_MODES,
    AUTH_MODE_AUTHORIZATION_CODE,
    AUTH_MODE_CLIENT_CREDENTIALS,
    AUTH_MODE_JWT_BEARER,
    AUTH_MODE_STATIC,
    OUTBOUND_ONLY_MODES,
    UnknownConnectorError,
    UnsupportedAuthModeError,
    connector_supports_mode,
    get_default_auth_mode,
    get_supported_auth_modes,
    resolve_auth_mode,
    set_auth_mode,
)
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.models import ConnectorAuthConfig

_VAULT_KEY = Fernet.generate_key().decode()
BACKEND_DIR = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The four modes + the outbound-only set
# ---------------------------------------------------------------------------


def test_all_four_modes_are_recognised():
    assert ALL_AUTH_MODES == {
        AUTH_MODE_AUTHORIZATION_CODE,
        AUTH_MODE_CLIENT_CREDENTIALS,
        AUTH_MODE_JWT_BEARER,
        AUTH_MODE_STATIC,
    }


def test_outbound_only_modes_exclude_authorization_code():
    # Only authorization_code needs an inbound callback; the other three are the
    # no-public-inbound options (R18-A3 §1).
    assert OUTBOUND_ONLY_MODES == {
        AUTH_MODE_CLIENT_CREDENTIALS,
        AUTH_MODE_JWT_BEARER,
        AUTH_MODE_STATIC,
    }
    assert AUTH_MODE_AUTHORIZATION_CODE not in OUTBOUND_ONLY_MODES
    assert OUTBOUND_ONLY_MODES <= ALL_AUTH_MODES


# ---------------------------------------------------------------------------
# Every connector registers its supported modes; default == flow
# ---------------------------------------------------------------------------


def test_every_oauth_config_declares_supported_modes():
    for connector_id, config in CONNECTOR_AUTH_CONFIGS.items():
        assert config.supported_auth_modes, (
            f"{connector_id} must declare supported_auth_modes"
        )
        # Every declared mode is a recognised AuthMode.
        for mode in config.supported_auth_modes:
            assert mode in ALL_AUTH_MODES, f"{connector_id}: unknown mode {mode!r}"


def test_default_mode_matches_the_oauth_flow():
    # The first (default) supported mode must equal the connector's OAuth grant,
    # so selecting nothing preserves today's live behaviour.
    for connector_id, config in CONNECTOR_AUTH_CONFIGS.items():
        assert config.supported_auth_modes[0] == config.flow, connector_id
        assert get_default_auth_mode(connector_id) == config.flow


def test_servicenow_jira_confluence_also_support_static():
    # These three have both an OAuth flow and the R17-D3 static-credential path.
    for connector_id in ("servicenow", "jira", "confluence"):
        modes = get_supported_auth_modes(connector_id)
        assert AUTH_MODE_AUTHORIZATION_CODE in modes
        assert AUTH_MODE_STATIC in modes


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def test_supported_modes_for_static_only_connectors():
    for connector_id in ("postgresql", "sql_server", "sqlserver", "oracle_db"):
        assert get_supported_auth_modes(connector_id) == (AUTH_MODE_STATIC,)
        assert get_default_auth_mode(connector_id) == AUTH_MODE_STATIC


def test_supported_modes_for_client_credentials_connectors():
    for connector_id in ("sap", "dynamics365"):
        assert get_supported_auth_modes(connector_id) == (AUTH_MODE_CLIENT_CREDENTIALS,)


def test_unknown_connector_has_no_modes_and_raises_for_default():
    assert get_supported_auth_modes("does_not_exist") == ()
    assert connector_supports_mode("does_not_exist", AUTH_MODE_STATIC) is False
    with pytest.raises(UnknownConnectorError):
        get_default_auth_mode("does_not_exist")


def test_connector_supports_mode():
    assert connector_supports_mode("jira", AUTH_MODE_STATIC) is True
    assert connector_supports_mode("jira", AUTH_MODE_AUTHORIZATION_CODE) is True
    # jwt_bearer is not wired for Jira (and never will be) — not supported.
    assert connector_supports_mode("jira", AUTH_MODE_JWT_BEARER) is False
    # Salesforce advertises jwt_bearer once its flow is wired (AT-555).
    assert connector_supports_mode("salesforce", AUTH_MODE_JWT_BEARER) is True


def test_empty_supported_modes_falls_back_to_flow(monkeypatch):
    """A config predating supported_auth_modes still resolves via its flow."""
    legacy = ConnectorAuthConfig(
        connector_id="legacy_cc",
        flow="client_credentials",
        client_id="x",
        secret_key="LEGACY_CC_CLIENT_SECRET",
        token_url="https://example.test/token",
        scopes=["default"],
        # supported_auth_modes deliberately left at its empty default.
    )
    monkeypatch.setitem(CONNECTOR_AUTH_CONFIGS, "legacy_cc", legacy)
    assert get_supported_auth_modes("legacy_cc") == (AUTH_MODE_CLIENT_CREDENTIALS,)
    assert get_default_auth_mode("legacy_cc") == AUTH_MODE_CLIENT_CREDENTIALS


# ---------------------------------------------------------------------------
# Per-org selection: set / resolve / isolation / fallback
# ---------------------------------------------------------------------------


def _clear_mode(org_id: str, connector_id: str) -> None:
    record = db.org_connector_get(org_id, connector_id) or {}
    record.pop("auth_mode", None)
    db.org_connector_set(org_id, connector_id, record)


def test_resolve_defaults_when_nothing_selected():
    _clear_mode("org_modes_A", "jira")
    assert resolve_auth_mode("org_modes_A", "jira") == AUTH_MODE_AUTHORIZATION_CODE


def test_set_then_resolve_round_trip():
    returned = set_auth_mode("org_modes_A", "jira", AUTH_MODE_STATIC)
    assert returned == AUTH_MODE_STATIC
    assert resolve_auth_mode("org_modes_A", "jira") == AUTH_MODE_STATIC


def test_selection_is_per_org_isolated():
    set_auth_mode("org_modes_A", "servicenow", AUTH_MODE_STATIC)
    _clear_mode("org_modes_B", "servicenow")
    # org_A selected static; org_B, having selected nothing, still gets the default.
    assert resolve_auth_mode("org_modes_A", "servicenow") == AUTH_MODE_STATIC
    assert resolve_auth_mode("org_modes_B", "servicenow") == AUTH_MODE_AUTHORIZATION_CODE


def test_set_mode_preserves_other_connector_state():
    db.org_connector_set(
        "org_modes_C", "jira", {"id": "jira", "status": "connected", "lastSynced": "yesterday"}
    )
    set_auth_mode("org_modes_C", "jira", AUTH_MODE_STATIC)
    record = db.org_connector_get("org_modes_C", "jira")
    assert record["status"] == "connected"
    assert record["lastSynced"] == "yesterday"
    assert record["auth_mode"] == AUTH_MODE_STATIC


def test_set_unsupported_mode_rejected():
    with pytest.raises(UnsupportedAuthModeError):
        set_auth_mode("org_modes_A", "jira", AUTH_MODE_JWT_BEARER)


def test_set_unknown_connector_rejected():
    with pytest.raises(UnknownConnectorError):
        set_auth_mode("org_modes_A", "does_not_exist", AUTH_MODE_STATIC)


def test_resolve_falls_back_when_stored_mode_no_longer_supported():
    # Simulate a mode that was valid once but has since been retired for this
    # connector: it is stored raw, and resolve must degrade to the default rather
    # than return an unsupported value.
    db.org_connector_set(
        "org_modes_D", "jira", {"id": "jira", "auth_mode": AUTH_MODE_JWT_BEARER}
    )
    assert resolve_auth_mode("org_modes_D", "jira") == AUTH_MODE_AUTHORIZATION_CODE


# ---------------------------------------------------------------------------
# AC3 — every mode resolves through get_connector_credentials() identically,
#        and no ingestion code branches on auth mode.
# ---------------------------------------------------------------------------


def test_get_connector_credentials_is_mode_agnostic(monkeypatch):
    """Both an OAuth token (authorization_code/client_credentials) and a static
    credential resolve through the SAME get_connector_credentials() call with no
    mode argument — the resolution never asks which mode produced the credential."""
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    from app.auth import vault
    from app.auth.credentials import get_connector_credentials
    from app.auth.models import StaticCredentialRecord, TokenRecord

    org = "org_modes_ac3"

    # An OAuth-mode connector terminates in a TokenRecord...
    vault.store_token(
        org,
        "salesforce",
        {"access_token": "FAKE-oauth-access-token", "expires_in": 3600, "scope": "api"},
    )
    # ...a static-mode connector terminates in a StaticCredentialRecord...
    vault.store_static_credential(
        org,
        "jira",
        username="svc@example.com",
        secret="FAKE-jira-api-token",
        base_url="https://example.atlassian.net",
    )

    # ...and the SAME resolution call — no mode parameter — returns each.
    oauth_cred = get_connector_credentials(org, "salesforce")
    static_cred = get_connector_credentials(org, "jira")
    assert isinstance(oauth_cred, TokenRecord)
    assert isinstance(static_cred, StaticCredentialRecord)
    # The credential a connector holds (its access token / secret) resolves the
    # same way regardless of which mode produced it — that is the AC3 guarantee.
    assert oauth_cred.access_token == "FAKE-oauth-access-token"
    assert static_cred.secret == "FAKE-jira-api-token"


def test_no_ingestion_module_branches_on_auth_mode():
    """AC3: the mode concept lives at the auth edge only. No ingestion or native
    connector module may reference auth_mode / the auth_modes helpers — if one did,
    ingestion would be branching on mode instead of resolving mode-agnostically."""
    scanned_roots = [
        BACKEND_DIR / "discovery" / "ingest",
        BACKEND_DIR / "connectors",
    ]
    needles = ("auth_mode", "supported_auth_modes", "resolve_auth_mode")
    offenders: list[str] = []
    for root in scanned_roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                offenders.append(str(path.relative_to(BACKEND_DIR)))
    assert not offenders, (
        "ingestion/connector code must not branch on auth mode (AC3): " + ", ".join(offenders)
    )
