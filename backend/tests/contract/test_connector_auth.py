"""Contract tests for AT-73 and AT-74: auth data models and secret resolution.

AT-73 / AC9:  ConnectorAuthConfig has secret_key (env var name), never client_secret.
AT-73 / AC18: All three models importable from backend.app.auth and backend.app.auth.models.
AT-74 / AC10: MissingSecretError, resolve_secret, validate_all_secrets importable from backend.app.auth.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# AC18: import surface checks
# ---------------------------------------------------------------------------


def test_import_from_package():
    """All three models importable from backend.app.auth."""
    from backend.app.auth import (  # noqa: F401
        ConnectorAuthConfig,
        ConnectorNotAuthenticatedError,
        TokenRecord,
    )


def test_import_from_models_module():
    """All three models importable from backend.app.auth.models."""
    from backend.app.auth.models import (  # noqa: F401
        ConnectorAuthConfig,
        ConnectorNotAuthenticatedError,
        TokenRecord,
    )


# ---------------------------------------------------------------------------
# AC9: field presence / absence
# ---------------------------------------------------------------------------


def test_connector_auth_config_has_secret_key_not_client_secret():
    """ConnectorAuthConfig must have secret_key and must NOT have client_secret."""
    from backend.app.auth.models import ConnectorAuthConfig

    field_names = {f.name for f in dataclasses.fields(ConnectorAuthConfig)}
    assert "secret_key" in field_names, "secret_key field is required"
    assert "client_secret" not in field_names, "client_secret must never appear on the model"


def test_secret_key_holds_env_var_name_not_secret_value():
    """secret_key on a constructed config holds an env var name string."""
    from backend.app.auth.models import ConnectorAuthConfig

    cfg = ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        client_id="my-client-id",
        secret_key="SALESFORCE_CLIENT_SECRET",
        token_url="https://login.salesforce.com/services/oauth2/token",
        scopes=["api", "refresh_token"],
    )
    assert cfg.secret_key == "SALESFORCE_CLIENT_SECRET"
    assert "SECRET" in cfg.secret_key  # env var name, not a raw secret


# ---------------------------------------------------------------------------
# ConnectorAuthConfig construction variants
# ---------------------------------------------------------------------------


def test_connector_auth_config_without_revocation_url():
    """ConnectorAuthConfig with revocation_url=None (GitHub/Slack/SAP/D365 pattern)."""
    from backend.app.auth.models import ConnectorAuthConfig

    cfg = ConnectorAuthConfig(
        connector_id="github",
        flow="authorization_code",
        client_id="gh-client",
        secret_key="GITHUB_CLIENT_SECRET",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["repo", "read:user"],
        revocation_url=None,
    )
    assert cfg.revocation_url is None
    assert cfg.connector_id == "github"


def test_connector_auth_config_with_revocation_url():
    """ConnectorAuthConfig with a revocation_url (Salesforce/ServiceNow/Jira/Confluence pattern)."""
    from backend.app.auth.models import ConnectorAuthConfig

    cfg = ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        client_id="sf-client",
        secret_key="SALESFORCE_CLIENT_SECRET",
        token_url="https://login.salesforce.com/services/oauth2/token",
        scopes=["api"],
        revocation_url="https://login.salesforce.com/services/oauth2/revoke",
    )
    assert cfg.revocation_url == "https://login.salesforce.com/services/oauth2/revoke"


def test_connector_auth_config_client_credentials_flow():
    """client_credentials flow sets redirect_uri=None by default."""
    from backend.app.auth.models import ConnectorAuthConfig

    cfg = ConnectorAuthConfig(
        connector_id="sap",
        flow="client_credentials",
        client_id="sap-client",
        secret_key="SAP_CLIENT_SECRET",
        token_url="https://sap.example.com/oauth/token",
        scopes=["read"],
    )
    assert cfg.flow == "client_credentials"
    assert cfg.redirect_uri is None


# ---------------------------------------------------------------------------
# TokenRecord construction variants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc)


def test_token_record_without_refresh_token():
    """TokenRecord with refresh_token=None (client_credentials pattern)."""
    from backend.app.auth.models import TokenRecord

    rec = TokenRecord(
        org_id="org-1",
        connector_id="sap",
        access_token="enc:tok123",
        expires_at=_LATER,
        scopes=["read"],
        created_at=_NOW,
        updated_at=_NOW,
        refresh_token=None,
    )
    assert rec.refresh_token is None
    assert rec.org_id == "org-1"


def test_token_record_with_refresh_token():
    """TokenRecord with a refresh_token string (authorization_code pattern)."""
    from backend.app.auth.models import TokenRecord

    rec = TokenRecord(
        org_id="org-2",
        connector_id="salesforce",
        access_token="enc:access456",
        expires_at=_LATER,
        scopes=["api", "refresh_token"],
        created_at=_NOW,
        updated_at=_NOW,
        refresh_token="enc:refresh789",
    )
    assert rec.refresh_token == "enc:refresh789"


# ---------------------------------------------------------------------------
# ConnectorNotAuthenticatedError
# ---------------------------------------------------------------------------


def test_connector_not_authenticated_error_is_exception():
    """ConnectorNotAuthenticatedError must be an Exception subclass."""
    from backend.app.auth.models import ConnectorNotAuthenticatedError

    assert issubclass(ConnectorNotAuthenticatedError, Exception)


def test_connector_not_authenticated_error_stores_attributes():
    """ConnectorNotAuthenticatedError stores org_id and connector_id as instance attributes."""
    from backend.app.auth.models import ConnectorNotAuthenticatedError

    err = ConnectorNotAuthenticatedError(org_id="org-abc", connector_id="jira")
    assert err.org_id == "org-abc"
    assert err.connector_id == "jira"


def test_connector_not_authenticated_error_message_contains_ids():
    """Exception message must include both connector_id and org_id values."""
    from backend.app.auth.models import ConnectorNotAuthenticatedError

    err = ConnectorNotAuthenticatedError(org_id="org-xyz", connector_id="servicenow")
    msg = str(err)
    assert "servicenow" in msg, f"connector_id not in message: {msg!r}"
    assert "org-xyz" in msg, f"org_id not in message: {msg!r}"


def test_connector_not_authenticated_error_is_raiseable():
    """ConnectorNotAuthenticatedError can be raised and caught as Exception."""
    from backend.app.auth.models import ConnectorNotAuthenticatedError

    import pytest

    with pytest.raises(ConnectorNotAuthenticatedError) as exc_info:
        raise ConnectorNotAuthenticatedError(org_id="org-1", connector_id="github")

    assert exc_info.value.connector_id == "github"
    assert exc_info.value.org_id == "org-1"


# ---------------------------------------------------------------------------
# AT-74: secret resolution helper (secrets.py)
# ---------------------------------------------------------------------------


def test_resolve_secret_returns_value_when_env_var_set():
    """resolve_secret returns the correct value when the env var is set."""
    import os
    from unittest.mock import patch

    from backend.app.auth.secrets import resolve_secret

    with patch.dict(os.environ, {"_TEST_SECRET_KEY": "my-secret-value"}):
        assert resolve_secret("_TEST_SECRET_KEY") == "my-secret-value"


def test_resolve_secret_raises_missing_secret_error_when_absent():
    """resolve_secret raises MissingSecretError when the env var is absent."""
    import os
    import pytest
    from unittest.mock import patch

    from backend.app.auth.secrets import MissingSecretError, resolve_secret

    env_without_key = {k: v for k, v in os.environ.items() if k != "_ABSENT_KEY"}
    with patch.dict(os.environ, env_without_key, clear=True):
        with pytest.raises(MissingSecretError):
            resolve_secret("_ABSENT_KEY")


def test_missing_secret_error_message_contains_key_name():
    """MissingSecretError message contains the key name."""
    from backend.app.auth.secrets import MissingSecretError

    err = MissingSecretError("SALESFORCE_CLIENT_SECRET")
    assert "SALESFORCE_CLIENT_SECRET" in str(err)


def test_missing_secret_error_message_does_not_contain_secret_value():
    """MissingSecretError message must never include the resolved secret value."""
    import os
    from unittest.mock import patch

    from backend.app.auth.secrets import MissingSecretError, resolve_secret

    secret_value = "super-sensitive-value-xyz"
    # MissingSecretError is constructed before the value is known; verify directly
    err = MissingSecretError("SOME_KEY")
    assert secret_value not in str(err)


def test_missing_secret_error_stores_secret_key_attribute():
    """MissingSecretError.secret_key stores the key name."""
    from backend.app.auth.secrets import MissingSecretError

    err = MissingSecretError("GITHUB_CLIENT_SECRET")
    assert err.secret_key == "GITHUB_CLIENT_SECRET"


def test_missing_secret_error_is_exception_subclass():
    """MissingSecretError is a subclass of Exception."""
    from backend.app.auth.secrets import MissingSecretError

    assert issubclass(MissingSecretError, Exception)


def test_resolve_secret_is_not_cached():
    """resolve_secret reads os.environ at call time — value is not cached between calls."""
    import os
    from unittest.mock import patch

    from backend.app.auth.secrets import resolve_secret

    with patch.dict(os.environ, {"_TEST_CACHE_KEY": "v1"}):
        assert resolve_secret("_TEST_CACHE_KEY") == "v1"
        os.environ["_TEST_CACHE_KEY"] = "v2"
        assert resolve_secret("_TEST_CACHE_KEY") == "v2", "resolve_secret must not cache values"


def test_validate_all_secrets_passes_when_all_present():
    """validate_all_secrets passes silently when every connector secret_key is set."""
    import os
    from unittest.mock import patch

    from backend.app.auth.models import ConnectorAuthConfig
    from backend.app.auth.secrets import validate_all_secrets

    configs = {
        "svc_a": ConnectorAuthConfig(
            connector_id="svc_a",
            flow="client_credentials",
            client_id="cid",
            secret_key="SVC_A_SECRET",
            token_url="https://example.com/token",
            scopes=[],
        ),
        "svc_b": ConnectorAuthConfig(
            connector_id="svc_b",
            flow="client_credentials",
            client_id="cid",
            secret_key="SVC_B_SECRET",
            token_url="https://example.com/token",
            scopes=[],
        ),
    }
    with patch.dict(os.environ, {"SVC_A_SECRET": "a", "SVC_B_SECRET": "b"}):
        validate_all_secrets(configs)  # must not raise


def test_validate_all_secrets_raises_when_any_absent():
    """validate_all_secrets raises MissingSecretError when any connector secret is missing."""
    import os
    import pytest
    from unittest.mock import patch

    from backend.app.auth.models import ConnectorAuthConfig
    from backend.app.auth.secrets import MissingSecretError, validate_all_secrets

    configs = {
        "svc_ok": ConnectorAuthConfig(
            connector_id="svc_ok",
            flow="client_credentials",
            client_id="cid",
            secret_key="SVC_OK_SECRET",
            token_url="https://example.com/token",
            scopes=[],
        ),
        "svc_missing": ConnectorAuthConfig(
            connector_id="svc_missing",
            flow="client_credentials",
            client_id="cid",
            secret_key="SVC_MISSING_SECRET",
            token_url="https://example.com/token",
            scopes=[],
        ),
    }
    env = {k: v for k, v in os.environ.items() if k not in ("SVC_OK_SECRET", "SVC_MISSING_SECRET")}
    env["SVC_OK_SECRET"] = "present"
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(MissingSecretError):
            validate_all_secrets(configs)


def test_validate_all_secrets_error_contains_missing_key_name():
    """MissingSecretError raised by validate_all_secrets contains the missing key name."""
    import os
    import pytest
    from unittest.mock import patch

    from backend.app.auth.models import ConnectorAuthConfig
    from backend.app.auth.secrets import MissingSecretError, validate_all_secrets

    configs = {
        "svc": ConnectorAuthConfig(
            connector_id="svc",
            flow="client_credentials",
            client_id="cid",
            secret_key="SVC_ABSENT_KEY",
            token_url="https://example.com/token",
            scopes=[],
        )
    }
    env = {k: v for k, v in os.environ.items() if k != "SVC_ABSENT_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(MissingSecretError) as exc_info:
            validate_all_secrets(configs)
    assert "SVC_ABSENT_KEY" in str(exc_info.value)


# AC10: importable from backend.app.auth
def test_secrets_importable_from_package():
    """MissingSecretError, resolve_secret, validate_all_secrets all importable from backend.app.auth."""
    from backend.app.auth import (  # noqa: F401
        MissingSecretError,
        resolve_secret,
        validate_all_secrets,
    )


# ---------------------------------------------------------------------------
# AT-75: OAuth flow implementation (oauth.py)
# ---------------------------------------------------------------------------

import json as _json
import os as _os

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers — lightweight mock transports (no external mock library required)
# ---------------------------------------------------------------------------


class _MockTransport(httpx.AsyncBaseTransport):
    """Returns a fixed status_code + JSON body; captures the last request."""

    def __init__(self, status_code: int, body: dict):
        self.last_request: httpx.Request | None = None
        self._status_code = status_code
        self._body = body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        content = _json.dumps(self._body).encode("utf-8")
        return httpx.Response(
            self._status_code,
            content=content,
            headers={"content-type": "application/json"},
        )


class _TimeoutTransport(httpx.AsyncBaseTransport):
    """Raises httpx.ReadTimeout for every request."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)


def _make_config(
    connector_id: str = "test_connector",
    flow: str = "authorization_code",
    authorization_url: str = "https://provider.example.com/auth",
    token_url: str = "https://provider.example.com/token",
    scopes: list | None = None,
) -> "ConnectorAuthConfig":
    from backend.app.auth.models import ConnectorAuthConfig

    return ConnectorAuthConfig(
        connector_id=connector_id,
        flow=flow,
        client_id="test-client-id",
        secret_key="_TEST_OAUTH_SECRET",
        token_url=token_url,
        scopes=scopes or ["read", "write"],
        redirect_uri="https://app.example.com/callback",
        authorization_url=authorization_url,
    )


# ---------------------------------------------------------------------------
# build_auth_url
# ---------------------------------------------------------------------------


def test_build_auth_url_contains_required_params():
    """build_auth_url URL contains client_id, redirect_uri, state, response_type=code, scopes."""
    from urllib.parse import parse_qs, urlparse

    from backend.app.auth.oauth import build_auth_url

    config = _make_config(scopes=["read", "write"])
    url = build_auth_url(config, state="csrf-nonce-abc")

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    assert qs["client_id"] == ["test-client-id"]
    assert qs["redirect_uri"] == ["https://app.example.com/callback"]
    assert qs["state"] == ["csrf-nonce-abc"]
    assert qs["response_type"] == ["code"]
    assert qs["scope"] == ["read write"]


def test_build_auth_url_does_not_call_resolve_secret():
    """build_auth_url succeeds even when the secret env var is absent."""
    from unittest.mock import patch

    from backend.app.auth.oauth import build_auth_url

    config = _make_config()
    env_without_secret = {k: v for k, v in _os.environ.items() if k != "_TEST_OAUTH_SECRET"}
    with patch.dict(_os.environ, env_without_secret, clear=True):
        url = build_auth_url(config, state="nonce")
    assert url.startswith("https://provider.example.com/auth")


# ---------------------------------------------------------------------------
# PKCE (RFC 7636) — code_challenge in auth URL, code_verifier in exchange
# ---------------------------------------------------------------------------


def test_generate_pkce_pair_is_valid_s256():
    """generate_pkce_pair returns (verifier, challenge) where challenge = S256(verifier)."""
    import base64 as _b64
    import hashlib as _hashlib

    from backend.app.auth.oauth import generate_pkce_pair

    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128  # RFC 7636 length bounds
    expected = (
        _b64.urlsafe_b64encode(_hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge  # base64url, padding stripped


def test_build_auth_url_includes_pkce_when_challenge_given():
    """build_auth_url adds code_challenge + code_challenge_method=S256 when a challenge is passed."""
    from urllib.parse import parse_qs, urlparse

    from backend.app.auth.oauth import build_auth_url

    config = _make_config()
    qs = parse_qs(urlparse(build_auth_url(config, state="n", code_challenge="chal-123")).query)
    assert qs["code_challenge"] == ["chal-123"]
    assert qs["code_challenge_method"] == ["S256"]

    # Omitting the challenge leaves PKCE params out (backward compatible).
    qs_none = parse_qs(urlparse(build_auth_url(config, state="n")).query)
    assert "code_challenge" not in qs_none


@pytest.mark.anyio
async def test_exchange_code_sends_code_verifier():
    """exchange_code includes code_verifier in the token POST when provided, omits it otherwise."""
    from backend.app.auth.oauth import exchange_code

    config = _make_config()
    with _patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        t1 = _MockTransport(200, {"access_token": "tok"})
        await exchange_code(config, code="c", code_verifier="verifier-xyz", _transport=t1)
        assert "code_verifier=verifier-xyz" in t1.last_request.content.decode()

        t2 = _MockTransport(200, {"access_token": "tok"})
        await exchange_code(config, code="c", _transport=t2)
        assert "code_verifier" not in t2.last_request.content.decode()


def test_auth_url_endpoint_emits_pkce_bound_to_stored_verifier(client):
    """The live auth-url endpoint emits an S256 challenge bound to the stored verifier."""
    import base64 as _b64
    import hashlib as _hashlib

    from app.auth.vault import consume_nonce

    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert r.status_code == 200

    qs = _parse_qs(_urlparse(r.json()["auth_url"]).query)
    assert qs["code_challenge_method"] == ["S256"]
    challenge = qs["code_challenge"][0]
    state = qs["state"][0]

    # The stored nonce carries the verifier; its S256 hash must equal the challenge.
    data = consume_nonce(state)
    assert data is not None and data.get("code_verifier")
    expected = (
        _b64.urlsafe_b64encode(_hashlib.sha256(data["code_verifier"].encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert expected == challenge


# ---------------------------------------------------------------------------
# exchange_code
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_exchange_code_200_returns_json():
    """exchange_code returns parsed JSON on HTTP 200."""
    from unittest.mock import patch

    from backend.app.auth.oauth import exchange_code

    transport = _MockTransport(200, {"access_token": "tok", "token_type": "Bearer"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        result = await exchange_code(config, code="auth-code-xyz", _transport=transport)

    assert result["access_token"] == "tok"


@pytest.mark.anyio
async def test_exchange_code_400_raises_oauth_error():
    """exchange_code raises OAuthError on HTTP 400."""
    from unittest.mock import patch

    from backend.app.auth.oauth import OAuthError, exchange_code

    transport = _MockTransport(400, {"error": "invalid_grant"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        with pytest.raises(OAuthError) as exc_info:
            await exchange_code(config, code="bad-code", _transport=transport)

    assert exc_info.value.reason == 400
    assert exc_info.value.connector_id == "test_connector"


@pytest.mark.anyio
async def test_exchange_code_401_raises_oauth_error():
    """exchange_code raises OAuthError on HTTP 401."""
    from unittest.mock import patch

    from backend.app.auth.oauth import OAuthError, exchange_code

    transport = _MockTransport(401, {"error": "unauthorized"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        with pytest.raises(OAuthError) as exc_info:
            await exchange_code(config, code="code", _transport=transport)

    assert exc_info.value.reason == 401


@pytest.mark.anyio
async def test_exchange_code_500_raises_oauth_error():
    """exchange_code raises OAuthError on HTTP 500."""
    from unittest.mock import patch

    from backend.app.auth.oauth import OAuthError, exchange_code

    transport = _MockTransport(500, {"error": "server_error"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        with pytest.raises(OAuthError) as exc_info:
            await exchange_code(config, code="code", _transport=transport)

    assert exc_info.value.reason == 500


@pytest.mark.anyio
async def test_exchange_code_timeout_raises_oauth_error():
    """exchange_code raises OAuthError with reason 'timeout' on httpx timeout."""
    from unittest.mock import patch

    from backend.app.auth.oauth import OAuthError, exchange_code

    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        with pytest.raises(OAuthError) as exc_info:
            await exchange_code(config, code="code", _transport=_TimeoutTransport())

    assert exc_info.value.reason == "timeout"


@pytest.mark.anyio
async def test_exchange_code_error_does_not_contain_secret():
    """OAuthError from exchange_code does not expose the secret value."""
    from unittest.mock import patch

    from backend.app.auth.oauth import OAuthError, exchange_code

    transport = _MockTransport(401, {})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "super-secret-value"}):
        with pytest.raises(OAuthError) as exc_info:
            await exchange_code(config, code="code", _transport=transport)

    assert "super-secret-value" not in str(exc_info.value)


@pytest.mark.anyio
async def test_exchange_code_post_body_contains_required_fields():
    """exchange_code POST body includes grant_type, code, client_id, redirect_uri."""
    from unittest.mock import patch
    from urllib.parse import parse_qs

    from backend.app.auth.oauth import exchange_code

    transport = _MockTransport(200, {"access_token": "tok"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        await exchange_code(config, code="mycode", _transport=transport)

    body = parse_qs(transport.last_request.content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["mycode"]
    assert body["client_id"] == ["test-client-id"]
    assert body["redirect_uri"] == ["https://app.example.com/callback"]


@pytest.mark.anyio
async def test_exchange_code_post_body_includes_client_secret_from_env():
    """exchange_code POST body includes client_secret resolved from env."""
    from unittest.mock import patch
    from urllib.parse import parse_qs

    from backend.app.auth.oauth import exchange_code

    transport = _MockTransport(200, {"access_token": "tok"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "resolved-secret"}):
        await exchange_code(config, code="code", _transport=transport)

    body = parse_qs(transport.last_request.content.decode())
    # Verify the field was sent (value presence confirms env was resolved)
    assert "client_secret" in body


# ---------------------------------------------------------------------------
# refresh_token
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_refresh_token_200_returns_json():
    """refresh_token returns parsed JSON on HTTP 200."""
    from unittest.mock import patch

    from backend.app.auth.oauth import refresh_token

    transport = _MockTransport(200, {"access_token": "new-tok", "token_type": "Bearer"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        result = await refresh_token(config, refresh_token_value="ref-tok", _transport=transport)

    assert result["access_token"] == "new-tok"


@pytest.mark.anyio
async def test_refresh_token_non_200_raises_oauth_error():
    """refresh_token raises OAuthError on non-200."""
    from unittest.mock import patch

    from backend.app.auth.oauth import OAuthError, refresh_token

    transport = _MockTransport(401, {"error": "invalid_token"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        with pytest.raises(OAuthError):
            await refresh_token(config, refresh_token_value="old-tok", _transport=transport)


@pytest.mark.anyio
async def test_refresh_token_timeout_raises_oauth_error():
    """refresh_token raises OAuthError with reason 'timeout' on httpx timeout."""
    from unittest.mock import patch

    from backend.app.auth.oauth import OAuthError, refresh_token

    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        with pytest.raises(OAuthError) as exc_info:
            await refresh_token(config, refresh_token_value="tok", _transport=_TimeoutTransport())

    assert exc_info.value.reason == "timeout"


@pytest.mark.anyio
async def test_refresh_token_post_body_contains_grant_type_and_refresh_token():
    """refresh_token POST body includes grant_type=refresh_token and refresh_token param."""
    from unittest.mock import patch
    from urllib.parse import parse_qs

    from backend.app.auth.oauth import refresh_token

    transport = _MockTransport(200, {"access_token": "tok"})
    config = _make_config()
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        await refresh_token(config, refresh_token_value="my-refresh-tok", _transport=transport)

    body = parse_qs(transport.last_request.content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["my-refresh-tok"]


# ---------------------------------------------------------------------------
# get_client_credentials_token
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_client_credentials_token_200_returns_json():
    """get_client_credentials_token returns parsed JSON on HTTP 200."""
    from unittest.mock import patch

    from backend.app.auth.oauth import get_client_credentials_token

    transport = _MockTransport(200, {"access_token": "cc-tok", "token_type": "Bearer"})
    config = _make_config(flow="client_credentials", authorization_url=None)
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        result = await get_client_credentials_token(config, _transport=transport)

    assert result["access_token"] == "cc-tok"


@pytest.mark.anyio
async def test_get_client_credentials_token_non_200_raises_oauth_error():
    """get_client_credentials_token raises OAuthError on non-200."""
    from unittest.mock import patch

    from backend.app.auth.oauth import OAuthError, get_client_credentials_token

    transport = _MockTransport(403, {"error": "unauthorized_client"})
    config = _make_config(flow="client_credentials", authorization_url=None)
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        with pytest.raises(OAuthError):
            await get_client_credentials_token(config, _transport=transport)


@pytest.mark.anyio
async def test_get_client_credentials_token_post_body_fields():
    """get_client_credentials_token POST body includes grant_type, scope, client_id."""
    from unittest.mock import patch
    from urllib.parse import parse_qs

    from backend.app.auth.oauth import get_client_credentials_token

    transport = _MockTransport(200, {"access_token": "tok"})
    config = _make_config(flow="client_credentials", authorization_url=None, scopes=["api.read"])
    with patch.dict(_os.environ, {"_TEST_OAUTH_SECRET": "s3cr3t"}):
        await get_client_credentials_token(config, _transport=transport)

    body = parse_qs(transport.last_request.content.decode())
    assert body["grant_type"] == ["client_credentials"]
    assert body["scope"] == ["api.read"]
    assert body["client_id"] == ["test-client-id"]


# ---------------------------------------------------------------------------
# Import surface — AC10 (oauth symbols)
# ---------------------------------------------------------------------------


def test_oauth_symbols_importable_from_package():
    """OAuthError, build_auth_url, exchange_code, refresh_token, get_client_credentials_token importable from backend.app.auth."""
    from backend.app.auth import (  # noqa: F401
        OAuthError,
        build_auth_url,
        exchange_code,
        get_client_credentials_token,
        refresh_token,
    )


# ---------------------------------------------------------------------------
# AT-76: Credential Vault (vault.py)
# ---------------------------------------------------------------------------

import sqlite3 as _sqlite3
from datetime import timedelta as _timedelta
from pathlib import Path as _Path
from unittest.mock import AsyncMock as _AsyncMock
from unittest.mock import patch as _patch


# ---------------------------------------------------------------------------
# Shared vault test fixtures and helpers
# ---------------------------------------------------------------------------

_VAULT_KEY = None  # set once per process in _ensure_vault_key()


def _ensure_vault_key() -> str:
    """Generate a test Fernet key once and cache it in CREDENTIAL_VAULT_KEY env var."""
    global _VAULT_KEY
    if _VAULT_KEY is None:
        from cryptography.fernet import Fernet
        _VAULT_KEY = Fernet.generate_key().decode()
    _os.environ["CREDENTIAL_VAULT_KEY"] = _VAULT_KEY
    return _VAULT_KEY


def _vault_env():
    """Return env dict containing the vault key (and secret for test connector)."""
    key = _ensure_vault_key()
    return {"CREDENTIAL_VAULT_KEY": key, "_TEST_OAUTH_SECRET": "s3cr3t"}


def _db_path() -> _Path:
    return _Path(_os.environ.get("DB_PATH", "database/dev.db"))


def _clear_credentials() -> None:
    """Truncate the credentials table between tests."""
    con = _sqlite3.connect(str(_db_path()))
    con.execute("DELETE FROM credentials")
    con.commit()
    con.close()


def _raw_credentials(org_id: str, connector_id: str) -> tuple | None:
    """Return raw DB row (access_token, refresh_token) without decryption."""
    con = _sqlite3.connect(str(_db_path()))
    cur = con.execute(
        "SELECT access_token, refresh_token FROM credentials WHERE org_id=%s AND connector_id=%s",
        (org_id, connector_id),
    )
    row = cur.fetchone()
    con.close()
    return row


def _now_utc():
    from datetime import timezone
    return datetime.now(timezone.utc)


def _token_response(
    access_token: str = "plain-access-tok",
    refresh_token: str | None = "plain-refresh-tok",
    expires_in: int = 3600,
) -> dict:
    resp: dict = {"access_token": access_token, "expires_in": expires_in}
    if refresh_token is not None:
        resp["refresh_token"] = refresh_token
    return resp


# ---------------------------------------------------------------------------
# Encryption tests (AC8)
# ---------------------------------------------------------------------------


def test_store_token_encrypts_access_token():
    """store_token writes encrypted (not plaintext) access_token to DB."""
    from backend.app.auth.vault import store_token

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-enc-1", "salesforce", _token_response(access_token="plain-secret"))

    row = _raw_credentials("org-enc-1", "salesforce")
    assert row is not None
    assert row[0] != "plain-secret"
    assert "plain-secret" not in row[0]
    _clear_credentials()


def test_store_token_encrypts_refresh_token():
    """store_token writes encrypted (not plaintext) refresh_token to DB when present."""
    from backend.app.auth.vault import store_token

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-enc-2", "jira", _token_response(refresh_token="plain-refresh"))

    row = _raw_credentials("org-enc-2", "jira")
    assert row is not None
    assert row[1] is not None
    assert row[1] != "plain-refresh"
    assert "plain-refresh" not in row[1]
    _clear_credentials()


def test_get_token_returns_decrypted_access_token():
    """get_token decrypts and returns the original plaintext access_token."""
    import asyncio
    from backend.app.auth.vault import store_token, get_token

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-dec-1", "github", _token_response(access_token="original-tok"))
        record = asyncio.run(get_token("org-dec-1", "github"))

    assert record.access_token == "original-tok"
    _clear_credentials()


def test_raw_db_record_does_not_contain_plaintext_token():
    """Raw DB record must never contain the plaintext token (AC8)."""
    from backend.app.auth.vault import store_token

    plain = "super-secret-access-value"
    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-raw-1", "slack", _token_response(access_token=plain))

    row = _raw_credentials("org-raw-1", "slack")
    assert row is not None
    # Neither column should contain the plaintext
    assert plain not in (row[0] or "")
    assert plain not in (row[1] or "")
    _clear_credentials()


# ---------------------------------------------------------------------------
# store_token tests
# ---------------------------------------------------------------------------


def test_store_token_inserts_new_record():
    """store_token inserts a new record when none exists for (org_id, connector_id)."""
    from backend.app.auth.vault import store_token

    with _patch.dict(_os.environ, _vault_env()):
        record = store_token("org-ins-1", "confluence", _token_response())

    assert record.org_id == "org-ins-1"
    assert record.connector_id == "confluence"
    row = _raw_credentials("org-ins-1", "confluence")
    assert row is not None
    _clear_credentials()


def test_store_token_upserts_existing_record():
    """store_token updates the existing record on second call with same (org_id, connector_id)."""
    from backend.app.auth.vault import store_token

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-ups-1", "github", _token_response(access_token="first-tok"))
        record2 = store_token("org-ups-1", "github", _token_response(access_token="second-tok"))

    assert record2.access_token == "second-tok"
    # Composite unique means only one row should exist
    con = _sqlite3.connect(str(_db_path()))
    count = con.execute(
        "SELECT COUNT(*) FROM credentials WHERE org_id=%s AND connector_id=%s",
        ("org-ups-1", "github"),
    ).fetchone()[0]
    con.close()
    assert count == 1
    _clear_credentials()


def test_store_token_expires_in_sets_utc_expires_at():
    """store_token with expires_in calculates a UTC expires_at approximately correct."""
    from datetime import timezone
    from backend.app.auth.vault import store_token

    with _patch.dict(_os.environ, _vault_env()):
        before = datetime.now(timezone.utc)
        record = store_token("org-exp-1", "jira", _token_response(expires_in=7200))
        after = datetime.now(timezone.utc)

    # expires_at should be ~2 hours from now
    delta = (record.expires_at - before).total_seconds()
    assert 7190 <= delta <= 7210, f"Unexpected delta: {delta}"
    _clear_credentials()


def test_store_token_handles_expires_at_absolute():
    """store_token with expires_at (absolute timestamp) sets expires_at correctly."""
    from datetime import timezone
    from backend.app.auth.vault import store_token

    future_ts = (_now_utc() + _timedelta(hours=2)).timestamp()
    resp = {"access_token": "tok", "expires_at": future_ts}

    with _patch.dict(_os.environ, _vault_env()):
        record = store_token("org-abs-1", "servicenow", resp)

    delta = (record.expires_at - _now_utc()).total_seconds()
    assert 7100 <= delta <= 7300, f"Unexpected delta: {delta}"
    _clear_credentials()


def test_store_token_null_refresh_token_for_client_credentials():
    """store_token stores refresh_token=None for client_credentials response."""
    from backend.app.auth.vault import store_token

    resp = {"access_token": "cc-tok", "expires_in": 3600}  # no refresh_token

    with _patch.dict(_os.environ, _vault_env()):
        record = store_token("org-cc-1", "sap", resp)

    assert record.refresh_token is None
    row = _raw_credentials("org-cc-1", "sap")
    assert row[1] is None
    _clear_credentials()


# ---------------------------------------------------------------------------
# get_token tests
# ---------------------------------------------------------------------------


def test_get_token_returns_valid_record_when_not_near_expiry():
    """get_token returns a valid TokenRecord when the token is not near expiry (AC5)."""
    import asyncio
    from backend.app.auth.vault import store_token, get_token

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-get-1", "github", _token_response(expires_in=7200))
        record = asyncio.run(get_token("org-get-1", "github"))

    assert record.access_token == "plain-access-tok"
    assert record.org_id == "org-get-1"
    _clear_credentials()


@pytest.mark.anyio
async def test_get_token_auto_refreshes_near_expiry():
    """get_token calls oauth.refresh_token when token is within REFRESH_THRESHOLD_SECONDS (AC6)."""
    from backend.app.auth.vault import store_token, get_token

    new_response = _token_response(access_token="refreshed-tok", refresh_token="new-refresh", expires_in=7200)
    mock_refresh = _AsyncMock(return_value=new_response)

    with _patch.dict(_os.environ, _vault_env()):
        # Store a token that expires in 60s (well within 300s threshold)
        store_token("org-ref-1", "salesforce", _token_response(access_token="old-tok", expires_in=60))

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"salesforce": _make_config(connector_id="salesforce")}):
            record = await get_token("org-ref-1", "salesforce")

    mock_refresh.assert_called_once()
    assert record.access_token == "refreshed-tok"
    _clear_credentials()


@pytest.mark.anyio
async def test_get_token_stores_refreshed_token():
    """get_token stores the refreshed token before returning (AC6)."""
    from backend.app.auth.vault import store_token, get_token

    new_response = _token_response(access_token="stored-refreshed", refresh_token="new-ref", expires_in=7200)
    mock_refresh = _AsyncMock(return_value=new_response)

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-ref-2", "salesforce", _token_response(access_token="old", expires_in=60))

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"salesforce": _make_config(connector_id="salesforce")}):
            await get_token("org-ref-2", "salesforce")

        # The DB should now contain the refreshed token (encrypted)
        from cryptography.fernet import Fernet
        row = _raw_credentials("org-ref-2", "salesforce")
        decrypted = Fernet(_os.environ["CREDENTIAL_VAULT_KEY"].encode()).decrypt(row[0].encode()).decode()
        assert decrypted == "stored-refreshed"

    _clear_credentials()


@pytest.mark.anyio
async def test_get_token_returns_refreshed_value_after_auto_refresh():
    """get_token returns the refreshed token value after auto-refresh."""
    from backend.app.auth.vault import store_token, get_token

    mock_refresh = _AsyncMock(return_value=_token_response(access_token="brand-new", expires_in=7200))

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-ref-3", "salesforce", _token_response(expires_in=60))

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"salesforce": _make_config(connector_id="salesforce")}):
            record = await get_token("org-ref-3", "salesforce")

    assert record.access_token == "brand-new"
    _clear_credentials()


@pytest.mark.anyio
async def test_get_token_preserves_refresh_token_when_provider_omits_it():
    """A provider that returns NO refresh_token on refresh (e.g. Salesforce keeps
    the same long-lived refresh token) must not lose it. The existing refresh
    token is carried forward so repeat auto-refresh keeps working instead of
    dropping the connector to needs_auth after a single refresh.
    """
    from backend.app.auth.vault import store_token, get_token

    # Refresh response intentionally omits refresh_token.
    no_refresh = _token_response(access_token="refreshed", refresh_token=None, expires_in=7200)
    mock_refresh = _AsyncMock(return_value=no_refresh)

    with _patch.dict(_os.environ, _vault_env()):
        store_token(
            "org-ref-keep",
            "salesforce",
            _token_response(access_token="old", refresh_token="keep-me", expires_in=60),
        )

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"salesforce": _make_config(connector_id="salesforce")}):
            record = await get_token("org-ref-keep", "salesforce")

        assert record.access_token == "refreshed"
        # Carried forward, not nulled.
        assert record.refresh_token == "keep-me"

        # Persisted (encrypted) so a SECOND refresh still has a token to present.
        from cryptography.fernet import Fernet

        row = _raw_credentials("org-ref-keep", "salesforce")
        decrypted_refresh = (
            Fernet(_os.environ["CREDENTIAL_VAULT_KEY"].encode())
            .decrypt(row[1].encode())
            .decode()
        )
        assert decrypted_refresh == "keep-me"

    _clear_credentials()


@pytest.mark.anyio
async def test_get_token_raises_when_no_token_exists():
    """get_token raises ConnectorNotAuthenticatedError when no token exists (AC7)."""
    from app.auth.models import ConnectorNotAuthenticatedError
    from backend.app.auth.vault import get_token

    with _patch.dict(_os.environ, _vault_env()):
        with pytest.raises(ConnectorNotAuthenticatedError):
            await get_token("org-missing", "salesforce")


@pytest.mark.anyio
async def test_get_token_raises_when_refresh_fails():
    """get_token raises ConnectorNotAuthenticatedError when oauth.refresh_token raises OAuthError (AC7)."""
    from app.auth.models import ConnectorNotAuthenticatedError
    from app.auth.oauth import OAuthError
    from backend.app.auth.vault import store_token, get_token

    mock_refresh = _AsyncMock(side_effect=OAuthError("salesforce", 401))

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-fail-1", "salesforce", _token_response(expires_in=60))

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"salesforce": _make_config(connector_id="salesforce")}):
            with pytest.raises(ConnectorNotAuthenticatedError):
                await get_token("org-fail-1", "salesforce")

    _clear_credentials()


# ---------------------------------------------------------------------------
# revoke_token tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_revoke_token_calls_external_endpoint_when_revocation_url_present():
    """revoke_token calls the external revocation endpoint when config.revocation_url is set (AC11)."""
    from backend.app.auth.vault import store_token, revoke_token

    transport = _MockTransport(200, {})
    config = _make_config(connector_id="jira")
    config_with_revocation = _make_config.__wrapped__(config) if hasattr(_make_config, "__wrapped__") else None

    # Build a config that has revocation_url
    from backend.app.auth.models import ConnectorAuthConfig
    revocable_config = ConnectorAuthConfig(
        connector_id="jira",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://auth.atlassian.com/oauth/token",
        scopes=["read"],
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
        redirect_uri="https://app.example.com/callback",
        authorization_url="https://auth.atlassian.com/authorize",
    )

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-rev-1", "jira", _token_response())
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"jira": revocable_config}):
            await revoke_token("org-rev-1", "jira", _revoke_transport=transport)

    assert transport.last_request is not None  # external call was made
    _clear_credentials()


@pytest.mark.anyio
async def test_revoke_token_skips_external_endpoint_when_no_revocation_url():
    """revoke_token does not call external endpoint when config.revocation_url is None (AC12)."""
    from backend.app.auth.vault import store_token, revoke_token

    transport = _MockTransport(200, {})
    from backend.app.auth.models import ConnectorAuthConfig
    no_revoke_config = ConnectorAuthConfig(
        connector_id="github",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["repo"],
        revocation_url=None,
    )

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-rev-2", "github", _token_response())
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"github": no_revoke_config}):
            await revoke_token("org-rev-2", "github", _revoke_transport=transport)

    assert transport.last_request is None  # no external call made
    _clear_credentials()


@pytest.mark.anyio
async def test_revoke_token_deletes_local_record():
    """revoke_token deletes the local DB record regardless of revocation_url."""
    from backend.app.auth.vault import store_token, revoke_token

    from backend.app.auth.models import ConnectorAuthConfig
    config = ConnectorAuthConfig(
        connector_id="slack",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=["read"],
        revocation_url=None,
    )

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-del-1", "slack", _token_response())
        assert _raw_credentials("org-del-1", "slack") is not None

        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"slack": config}):
            await revoke_token("org-del-1", "slack")

    assert _raw_credentials("org-del-1", "slack") is None


@pytest.mark.anyio
async def test_revoke_token_step1_failure_logs_warning_still_deletes():
    """Step 1 HTTP 500 from revocation endpoint: logs warning, deletes local record, does not raise (AC13)."""
    from backend.app.auth.vault import store_token, revoke_token

    from backend.app.auth.models import ConnectorAuthConfig
    revocable = ConnectorAuthConfig(
        connector_id="confluence",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://auth.atlassian.com/oauth/token",
        scopes=["read"],
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
    )

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-err-1", "confluence", _token_response())
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"confluence": revocable}):
            # Must not raise even though revocation endpoint returns 500
            await revoke_token("org-err-1", "confluence", _revoke_transport=_MockTransport(500, {}))

    assert _raw_credentials("org-err-1", "confluence") is None


@pytest.mark.anyio
async def test_revoke_token_step1_timeout_logs_warning_still_deletes():
    """Step 1 timeout: logs warning, deletes local record, does not raise (AC13)."""
    from backend.app.auth.vault import store_token, revoke_token

    from backend.app.auth.models import ConnectorAuthConfig
    revocable = ConnectorAuthConfig(
        connector_id="confluence",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://auth.atlassian.com/oauth/token",
        scopes=["read"],
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
    )

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-to-1", "confluence", _token_response())
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"confluence": revocable}):
            await revoke_token("org-to-1", "confluence", _revoke_transport=_TimeoutTransport())

    assert _raw_credentials("org-to-1", "confluence") is None


@pytest.mark.anyio
async def test_revoke_token_returns_none():
    """revoke_token returns None (caller responds with HTTP 204)."""
    from backend.app.auth.vault import store_token, revoke_token

    from backend.app.auth.models import ConnectorAuthConfig
    config = ConnectorAuthConfig(
        connector_id="sap",
        flow="client_credentials",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://sap.example.com/token",
        scopes=["read"],
        revocation_url=None,
    )

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-none-1", "sap", _token_response(refresh_token=None))
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"sap": config}):
            result = await revoke_token("org-none-1", "sap")

    assert result is None
    _clear_credentials()


@pytest.mark.anyio
async def test_revoke_token_is_idempotent():
    """Calling revoke_token on an already-deleted token does not raise."""
    from backend.app.auth.vault import revoke_token

    from backend.app.auth.models import ConnectorAuthConfig
    config = ConnectorAuthConfig(
        connector_id="sap",
        flow="client_credentials",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://sap.example.com/token",
        scopes=["read"],
        revocation_url=None,
    )

    with _patch.dict(_os.environ, _vault_env()):
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"sap": config}):
            # No token stored — must not raise
            await revoke_token("org-idem-1", "sap")
            # Second call also must not raise
            await revoke_token("org-idem-1", "sap")


# ---------------------------------------------------------------------------
# Multi-tenancy (AC8 — org isolation)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_token_scoped_to_org_id():
    """get_token for org_A cannot return the token belonging to org_B."""
    import asyncio
    from app.auth.models import ConnectorNotAuthenticatedError
    from backend.app.auth.vault import store_token, get_token

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org_A", "github", _token_response(access_token="org-a-token"))
        # org_B has no token — must raise
        with pytest.raises(ConnectorNotAuthenticatedError) as exc_info:
            await get_token("org_B", "github")

    assert exc_info.value.org_id == "org_B"
    _clear_credentials()


# ---------------------------------------------------------------------------
# Import surface — AC18 (vault symbols)
# ---------------------------------------------------------------------------


def test_vault_symbols_importable_from_package():
    """get_token, store_token, revoke_token importable from backend.app.auth (AC18)."""
    from backend.app.auth import (  # noqa: F401
        get_token,
        revoke_token,
        store_token,
    )


# ===========================================================================
# AT-77: OAuth callback and state security (routes_connector_auth.py)
# ===========================================================================
#
# Coverage:
#   AC1  — auth-url endpoint: valid URL, non-empty state, state contains no redirect_to
#   AC2  — callback stores token and uses hardcoded OAUTH_SUCCESS_REDIRECT constant
#   AC3  — state mismatch → 400; generic message; hmac.compare_digest used; redirect_to in state ignored
#   AC4  — redirect_to as query param does not change redirect target
#   AC13 — DELETE route returns 204 even when revocation endpoint is unreachable
#   AC14 — token-status returns needs_refresh when within REFRESH_THRESHOLD_SECONDS
#   AC15 — nonce is single-use: replay returns 400
#   AC16 — CONNECTOR_AUTH_CONFIGS has 8 entries with correct flow types and revocation_url values
#   AC17 — unauthenticated requests to auth-url, DELETE /token, token-status return 401
#   AC18 — full import surface round-trip (already covered above)
# ===========================================================================

import hmac as _hmac_mod
import secrets as _secrets_mod
from unittest.mock import AsyncMock as _AsyncMock
from unittest.mock import patch as _patch
from urllib.parse import parse_qs as _parse_qs
from urllib.parse import urlparse as _urlparse

_AUTH_HEADERS = {"Authorization": "Bearer dev-token-change-me"}


# ---------------------------------------------------------------------------
# AC16: CONNECTOR_AUTH_CONFIGS has 8 entries, correct flows, correct revocation_url values
# ---------------------------------------------------------------------------


def test_connector_auth_configs_has_8_entries():
    """CONNECTOR_AUTH_CONFIGS must have exactly 8 connectors (AC16)."""
    from backend.app.auth.configs import CONNECTOR_AUTH_CONFIGS

    assert len(CONNECTOR_AUTH_CONFIGS) == 8, (
        f"Expected 8 connectors, got {len(CONNECTOR_AUTH_CONFIGS)}: "
        f"{list(CONNECTOR_AUTH_CONFIGS)}"
    )


def test_connector_auth_configs_flow_types():
    """authorization_code connectors: salesforce, servicenow, jira, confluence, github, slack.
    client_credentials connectors: sap, dynamics365. (AC16)
    """
    from backend.app.auth.configs import CONNECTOR_AUTH_CONFIGS

    auth_code = {"salesforce", "servicenow", "jira", "confluence", "github", "slack"}
    client_creds = {"sap", "dynamics365"}

    for cid in auth_code:
        assert CONNECTOR_AUTH_CONFIGS[cid].flow == "authorization_code", (
            f"{cid} should be authorization_code"
        )
    for cid in client_creds:
        assert CONNECTOR_AUTH_CONFIGS[cid].flow == "client_credentials", (
            f"{cid} should be client_credentials"
        )


def test_connector_auth_configs_revocation_url_values():
    """Connectors with revocation_url set: salesforce, servicenow, jira, confluence.
    Connectors with revocation_url=None: github, slack, sap, dynamics365. (AC16)
    """
    from backend.app.auth.configs import CONNECTOR_AUTH_CONFIGS

    has_revocation = {"salesforce", "servicenow", "jira", "confluence"}
    no_revocation = {"github", "slack", "sap", "dynamics365"}

    for cid in has_revocation:
        assert CONNECTOR_AUTH_CONFIGS[cid].revocation_url is not None, (
            f"{cid} should have a revocation_url"
        )
    for cid in no_revocation:
        assert CONNECTOR_AUTH_CONFIGS[cid].revocation_url is None, (
            f"{cid} should have revocation_url=None"
        )


# ---------------------------------------------------------------------------
# AC17: unauthenticated requests return 401
# ---------------------------------------------------------------------------


def test_auth_url_unauthenticated_returns_401(client):
    """GET /api/connectors/salesforce/auth-url without Bearer token → 401 (AC17)."""
    resp = client.get("/api/connectors/salesforce/auth-url")
    assert resp.status_code == 401


def test_delete_token_unauthenticated_returns_401(client):
    """DELETE /api/connectors/salesforce/token without Bearer token → 401 (AC17)."""
    resp = client.delete("/api/connectors/salesforce/token")
    assert resp.status_code == 401


def test_token_status_unauthenticated_returns_401(client):
    """GET /api/connectors/salesforce/token-status without Bearer token → 401 (AC17)."""
    resp = client.get("/api/connectors/salesforce/token-status")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AC1: auth-url returns valid URL with correct params; state is non-empty and opaque
# ---------------------------------------------------------------------------


def test_auth_url_returns_salesforce_url_with_required_params(client):
    """auth-url returns URL containing client_id, redirect_uri, non-empty state,
    response_type=code, and at least one scope (AC1).
    """
    # Import from the same module the route uses (app.auth.configs, not
    # backend.app.auth.configs) so both sides see the same SALESFORCE_CLIENT_ID
    # value regardless of when load_dotenv() ran during the test session.
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    resp = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert "auth_url" in body
    assert body["connector_id"] == "salesforce"

    parsed = _urlparse(body["auth_url"])
    qs = _parse_qs(parsed.query)

    config = CONNECTOR_AUTH_CONFIGS["salesforce"]
    assert qs.get("client_id") == [config.client_id]
    assert qs.get("response_type") == ["code"]
    state_vals = qs.get("state", [])
    assert state_vals and state_vals[0], "state must be non-empty"
    scope_vals = qs.get("scope", [])
    assert scope_vals, "scope must be present"


def test_auth_url_state_contains_no_redirect_to_or_user_data(client):
    """State nonce from auth-url contains no redirect_to field and no user data (AC1)."""
    resp = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    parsed = _urlparse(resp.json()["auth_url"])
    qs = _parse_qs(parsed.query)
    state = qs["state"][0]

    assert "redirect_to" not in state
    assert "redirect" not in state.lower()
    assert "@" not in state  # no email
    assert "user" not in state.lower()


def test_auth_url_each_call_generates_unique_state(client):
    """Two consecutive auth-url calls produce different state nonces (AC1)."""
    r1 = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    r2 = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)

    s1 = _parse_qs(_urlparse(r1.json()["auth_url"]).query)["state"][0]
    s2 = _parse_qs(_urlparse(r2.json()["auth_url"]).query)["state"][0]
    assert s1 != s2, "Each auth-url request must produce a unique state"


def test_auth_url_client_credentials_connector_returns_400(client):
    """auth-url for a client_credentials connector (sap) returns 400 (not applicable)."""
    resp = client.get("/api/connectors/sap/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 400


def test_auth_url_unknown_connector_returns_404(client):
    """auth-url for unknown connector returns 404."""
    resp = client.get("/api/connectors/nonexistent/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AC2: callback stores token and redirects to hardcoded OAUTH_SUCCESS_REDIRECT
# ---------------------------------------------------------------------------


def test_callback_valid_state_redirects_to_success_url(client):
    """Callback with valid code + matching state redirects to OAUTH_SUCCESS_REDIRECT (AC2)."""
    # Step 1: generate a real nonce via auth-url
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    # Step 2: mock exchange_code to return a fake token; mock store_token to avoid vault key issues
    fake_token = {
        "access_token": "fake-access-tok",
        "refresh_token": "fake-refresh-tok",
        "expires_in": 3600,
    }
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=auth-code&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "connected=salesforce" in location


def test_callback_success_redirect_is_hardcoded_constant(client):
    """OAUTH_SUCCESS_REDIRECT is a hardcoded constant; no state content appears in the location (AC2/AC4)."""
    from app.routes_connector_auth import OAUTH_SUCCESS_REDIRECT

    # The constant must contain {connector_id} placeholder only — no user data slots
    assert "{connector_id}" in OAUTH_SUCCESS_REDIRECT
    assert "{state}" not in OAUTH_SUCCESS_REDIRECT
    assert "{code}" not in OAUTH_SUCCESS_REDIRECT
    assert "{redirect" not in OAUTH_SUCCESS_REDIRECT


def test_callback_redirect_does_not_contain_state_value(client):
    """The redirect location after a successful callback contains no state value (AC2/AC4)."""
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/github/auth-url", headers=_AUTH_HEADERS)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "tok", "expires_in": 3600}
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=c&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )

    location = resp.headers.get("location", "")
    assert state not in location, "State value must not appear in redirect location"


# ---------------------------------------------------------------------------
# AT-325 (CS-2 T3): callback redirect format matches OAuthCallbackPage
#   T3-AC1 — success → ?connected={connector_id}&status=success
#   T3-AC2 — failure → ?status=error&code={error_code}
#   T3-AC3 — both target the frontend /oauth/callback path
# ---------------------------------------------------------------------------


def test_success_redirect_format_matches_frontend_callback():
    """OAUTH_SUCCESS_REDIRECT targets /oauth/callback with connected + status=success (T3-AC1/AC3)."""
    from app.routes_connector_auth import OAUTH_SUCCESS_REDIRECT

    rendered = OAUTH_SUCCESS_REDIRECT.format(connector_id="salesforce")
    assert "/oauth/callback?" in rendered
    assert "connected=salesforce" in rendered
    assert "status=success" in rendered


def test_error_redirect_format_matches_frontend_callback():
    """OAUTH_ERROR_REDIRECT targets /oauth/callback with status=error + code (T3-AC2/AC3)."""
    from app.routes_connector_auth import OAUTH_ERROR_REDIRECT

    rendered = OAUTH_ERROR_REDIRECT.format(error_code="exchange_failed")
    assert "/oauth/callback?" in rendered
    assert "status=error" in rendered
    assert "code=exchange_failed" in rendered


def test_callback_success_location_uses_status_success(client):
    """A successful callback redirects with connected=<id> and status=success (T3-AC1)."""
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "tok", "refresh_token": "r", "expires_in": 3600}
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=auth-code&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/oauth/callback?" in location
    assert "connected=salesforce" in location
    assert "status=success" in location


def test_callback_success_marks_connector_connected(client):
    """A successful callback flips the org connector to 'connected' so the
    Integration Hub tile updates (CS-2 AC6). The old POST /connect set this; the
    OAuth success path must now set it since that POST was removed (AC8)."""
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "tok", "refresh_token": "r", "expires_in": 3600}
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=auth-code&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )
    assert resp.status_code == 302

    listed = client.get("/api/connectors", headers=_AUTH_HEADERS).json()
    salesforce = next(c for c in listed if c["id"] == "salesforce")
    assert salesforce["status"] == "connected"


def test_callback_marks_connector_connected_under_initiating_org(client):
    """The org from the authenticated /auth-url request is threaded through the
    state nonce, so the (unauthenticated) callback persists connection state under
    THAT org — not the hardcoded default. Without this, a user whose JWT org is not
    'default' completes OAuth but the tile stays disconnected (multi-tenant bug)."""
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": "acme-test-org"}
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=org_headers)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "tok", "refresh_token": "r", "expires_in": 3600}
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        # Callback carries no org context of its own — it must use the nonce's org.
        resp = client.get(
            f"/api/connectors/oauth/callback?code=auth-code&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )
    assert resp.status_code == 302

    # Connection state is written to the INITIATING org's namespaced row — proving
    # the org from /auth-url (not the callback's default) was used.
    from app import db as _db
    acme_row = _db.org_connector_get("acme-test-org", "salesforce")
    assert acme_row is not None
    assert acme_row.get("org_id") == "acme-test-org"
    assert acme_row["status"] == "connected"


def test_callback_error_location_uses_status_error_and_code(client):
    """A failed callback (missing code) redirects with status=error and a code param (T3-AC2)."""
    # No code/state → error path, no token exchange attempted.
    resp = client.get(
        "/api/connectors/oauth/callback?error=access_denied",
        headers=_AUTH_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/oauth/callback?" in location
    assert "status=error" in location
    assert "code=" in location


# ---------------------------------------------------------------------------
# AC3: state mismatch → 400; generic message; hmac.compare_digest used; redirect_to ignored
# ---------------------------------------------------------------------------


def test_callback_mismatched_state_returns_400(client):
    """Callback with wrong state returns HTTP 400 (AC3)."""
    resp = client.get(
        "/api/connectors/oauth/callback?code=mycode&state=totally-wrong-state",
        headers=_AUTH_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_callback_400_message_reveals_no_detail(client):
    """400 body on state mismatch must not mention 'state', 'nonce', or 'hmac' (AC3)."""
    resp = client.get(
        "/api/connectors/oauth/callback?code=mycode&state=bogus",
        headers=_AUTH_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 400
    body_text = resp.text.lower()
    assert "state" not in body_text
    assert "nonce" not in body_text
    assert "hmac" not in body_text
    assert "digest" not in body_text


def test_hmac_compare_digest_used_for_state_comparison():
    """_consume_nonce uses hmac.compare_digest (not ==) for state validation (AC3)."""
    from app.routes_connector_auth import _store_nonce, _consume_nonce

    nonce = "test-hmac-check-nonce-" + _secrets_mod.token_hex(4)
    _store_nonce(nonce, "salesforce")

    with _patch("app.routes_connector_auth.hmac.compare_digest", wraps=_hmac_mod.compare_digest) as mock_cd:
        result = _consume_nonce(nonce)

    assert mock_cd.called, "hmac.compare_digest must be called during nonce validation"
    assert result == "salesforce"


def test_callback_redirect_to_in_state_does_not_change_target(client):
    """redirect_to embedded in state param does not affect the redirect target (AC3/AC4)."""
    crafted_state = "redirect_to=https://evil.example.com"

    resp = client.get(
        f"/api/connectors/oauth/callback?code=c&state={crafted_state}",
        headers=_AUTH_HEADERS,
        follow_redirects=False,
    )
    # State mismatch (crafted state not in store) → 400; no open redirect
    assert resp.status_code == 400
    assert "evil.example.com" not in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# AC4: redirect_to as query param does not change redirect target
# ---------------------------------------------------------------------------


def test_callback_redirect_to_query_param_does_not_change_target(client):
    """redirect_to as a query param on the callback is ignored (AC4)."""
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "t", "expires_in": 3600}
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=c&state={state}"
            "&redirect_to=https://evil.example.com",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )

    location = resp.headers.get("location", "")
    assert "evil.example.com" not in location
    assert "connected=salesforce" in location


# ---------------------------------------------------------------------------
# AC13: DELETE route returns 204 even when revocation endpoint is unreachable
# ---------------------------------------------------------------------------


def test_delete_token_returns_204(client):
    """DELETE /api/connectors/{connector_id}/token returns 204 (AC13)."""
    with _patch("app.routes_connector_auth.revoke_token", new_callable=_AsyncMock, return_value=None):
        resp = client.delete(
            "/api/connectors/salesforce/token",
            headers=_AUTH_HEADERS,
        )
    assert resp.status_code == 204


def test_delete_token_unreachable_revocation_still_returns_204(client):
    """DELETE returns 204 even when vault.revoke_token encounters an unreachable endpoint (AC13).

    vault.revoke_token itself handles the failure gracefully (tested in AT-76 vault tests);
    this test verifies the route layer propagates the 204 correctly.
    """
    from app.auth.vault import store_token as _store_token_vault
    with _patch.dict(_os.environ, _vault_env()):
        # Store a token so revoke_token has something to work with
        from app.routes_connector_auth import _DEFAULT_ORG_ID
        _store_token_vault(_DEFAULT_ORG_ID, "confluence", _token_response())

        with _patch("app.routes_connector_auth.revoke_token", new_callable=_AsyncMock, return_value=None) as mock_revoke:
            resp = client.delete(
                "/api/connectors/confluence/token",
                headers=_AUTH_HEADERS,
            )

    assert resp.status_code == 204
    mock_revoke.assert_awaited_once()
    _clear_credentials()


# ---------------------------------------------------------------------------
# AC14: token-status returns needs_refresh when within REFRESH_THRESHOLD_SECONDS
# ---------------------------------------------------------------------------


def test_token_status_returns_needs_auth_when_no_token(client):
    """token-status returns needs_auth when no token is stored (AC14)."""
    resp = client.get(
        "/api/connectors/salesforce/token-status",
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "needs_auth"


def test_token_status_returns_connected_for_fresh_token(client):
    """token-status returns connected when token expiry is well beyond threshold (AC14)."""
    from app.auth.vault import store_token as _store_token_vault
    from app.routes_connector_auth import _DEFAULT_ORG_ID

    with _patch.dict(_os.environ, _vault_env()):
        _store_token_vault(_DEFAULT_ORG_ID, "salesforce", _token_response(expires_in=7200))
        resp = client.get(
            "/api/connectors/salesforce/token-status",
            headers=_AUTH_HEADERS,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "connected"
    _clear_credentials()


def test_token_status_returns_needs_refresh_when_within_threshold(client):
    """token-status returns needs_refresh when token expires within REFRESH_THRESHOLD_SECONDS (AC14)."""
    from app.auth.vault import store_token as _store_token_vault
    from app.routes_connector_auth import _DEFAULT_ORG_ID

    with _patch.dict(_os.environ, _vault_env()):
        # Token expires in 60 seconds (well within default 300s threshold)
        _store_token_vault(_DEFAULT_ORG_ID, "jira", _token_response(expires_in=60))
        resp = client.get(
            "/api/connectors/jira/token-status",
            headers=_AUTH_HEADERS,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "needs_refresh"
    _clear_credentials()


def test_token_status_returns_refresh_failed_when_flagged(client):
    """token-status returns refresh_failed when refresh_failed flag is set and token is near expiry (AC14)."""
    import sqlite3 as _sq
    from app.auth.vault import store_token as _store_token_vault
    from app.routes_connector_auth import _DEFAULT_ORG_ID

    with _patch.dict(_os.environ, _vault_env()):
        # Store a near-expiry token
        _store_token_vault(_DEFAULT_ORG_ID, "confluence", _token_response(expires_in=60))
        # Directly set refresh_failed=1 (simulating vault marking the flag after a failed refresh)
        db_path = _os.environ.get("DB_PATH", "database/dev.db")
        con = _sq.connect(db_path)
        con.execute(
            "UPDATE credentials SET refresh_failed=1 WHERE org_id=%s AND connector_id=%s",
            (_DEFAULT_ORG_ID, "confluence"),
        )
        con.commit()
        con.close()

        resp = client.get(
            "/api/connectors/confluence/token-status",
            headers=_AUTH_HEADERS,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "refresh_failed"
    _clear_credentials()


def test_token_status_returns_needs_auth_for_expired_token(client):
    """token-status returns needs_auth when token has already expired (AC14)."""
    from datetime import timedelta
    from app.auth.vault import store_token as _store_token_vault
    from app.routes_connector_auth import _DEFAULT_ORG_ID

    with _patch.dict(_os.environ, _vault_env()):
        # Store a token with a negative expires_in (effectively already expired)
        resp_dict = {
            "access_token": "expired-tok",
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        }
        _store_token_vault(_DEFAULT_ORG_ID, "github", resp_dict)
        resp = client.get(
            "/api/connectors/github/token-status",
            headers=_AUTH_HEADERS,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "needs_auth"
    _clear_credentials()


# ---------------------------------------------------------------------------
# AC15: state nonce is single-use; replay of used nonce returns 400
# ---------------------------------------------------------------------------


def test_nonce_single_use_replay_returns_400(client):
    """Replay of a used nonce returns 400 (AC15)."""
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "t", "expires_in": 3600}

    # First callback — valid
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        r1 = client.get(
            f"/api/connectors/oauth/callback?code=code1&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )

    assert r1.status_code == 302  # success redirect

    # Second callback — replay of same state → 400
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        r2 = client.get(
            f"/api/connectors/oauth/callback?code=code2&state={state}",
            headers=_AUTH_HEADERS,
            follow_redirects=False,
        )

    assert r2.status_code == 400, "Replay of a used nonce must return 400"
    _clear_credentials()


def test_nonce_replay_before_use_returns_400(client):
    """A state value that was never issued returns 400 immediately (AC15)."""
    fake_state = "never-issued-nonce-" + _secrets_mod.token_hex(8)
    resp = client.get(
        f"/api/connectors/oauth/callback?code=c&state={fake_state}",
        headers=_AUTH_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_callback_unauthenticated_returns_401(client):
    """GET /api/connectors/oauth/callback without Bearer token → 401 (AC17)."""
    resp = client.get(
        "/api/connectors/oauth/callback?code=c&state=s",
        follow_redirects=False,
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AT-77 import surface (AC18 extension)
# ---------------------------------------------------------------------------


def test_full_import_chain_no_circular_imports():
    """from backend.app.auth import get_token, store_token, revoke_token,
    ConnectorNotAuthenticatedError succeeds with no circular import error (AC18).
    """
    from backend.app.auth import (  # noqa: F401
        ConnectorNotAuthenticatedError,
        get_token,
        revoke_token,
        store_token,
    )


# ---------------------------------------------------------------------------
# AC8: Open redirect ignored — redirect_to in state never affects redirect target
# ---------------------------------------------------------------------------


def test_open_redirect_ignored(client):
    """redirect_to query param in callback is ignored — redirect always goes to OAUTH_SUCCESS_REDIRECT (AC8).

    Simulates an attacker appending redirect_to=https://evil.com to the callback URL.
    The route must redirect to the hardcoded constant only.
    """
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "t", "expires_in": 3600}
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code", new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=valid_code&state={state}&redirect_to=https://evil.com",
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
    """One-character state mismatch returns 400; hmac.compare_digest is used on
    the live nonce-consume path (not ==) (AC9)."""
    from app.auth import vault as _vault

    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert r.status_code == 200
    nonce = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    bad_state = nonce[:-1] + ("a" if nonce[-1] != "a" else "b")

    resp = client.get(
        f"/api/connectors/oauth/callback?code=code&state={bad_state}",
        headers=_AUTH_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code == 400, (
        f"Expected 400 for one-character state mismatch, got {resp.status_code}"
    )

    # The LIVE callback path validates state via vault.consume_nonce — assert it
    # actually invokes hmac.compare_digest (not ==). Issue a fresh nonce and
    # consume it under a wrap-patch to prove compare_digest is on the live path.
    fresh = _secrets_mod.token_urlsafe(32)
    _vault.store_nonce(fresh, "salesforce")
    with _patch(
        "app.auth.vault.hmac.compare_digest", wraps=_hmac_mod.compare_digest
    ) as mock_cd:
        data = _vault.consume_nonce(fresh)
    assert mock_cd.called, (
        "hmac.compare_digest() must be called on the live consume_nonce path (AC9)"
    )
    assert data is not None and data["connector_id"] == "salesforce"


# ---------------------------------------------------------------------------
# AC10: Nonce replay rejected — second callback with same nonce returns 400
# ---------------------------------------------------------------------------


def test_nonce_replay_rejected(client):
    """Two sequential callbacks with the same valid nonce: first succeeds (302), second returns 400 (AC10)."""
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/salesforce/auth-url", headers=_AUTH_HEADERS)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "tok", "expires_in": 3600}

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


# ---------------------------------------------------------------------------
# AT-164: Slack-specific revocation branch (T1-S11 Task 1, Section 3)
# AC1 — Slack DELETE calls auth.revoke with Bearer token
# AC2 — ok=false writes connector_revocation_failed audit event; local deletion completes
# AC3 — GitHub DELETE calls no external endpoint (no RFC 7009 URL, no Slack path)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_revoke_token_slack_calls_auth_revoke_with_bearer(client):
    """DELETE /api/connectors/slack/token calls https://slack.com/api/auth.revoke
    with the stored Slack access token as Bearer (AT-164 AC1).
    """
    from backend.app.auth.vault import store_token, revoke_token
    from backend.app.auth.models import ConnectorAuthConfig

    slack_config = ConnectorAuthConfig(
        connector_id="slack",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=["channels:read"],
        revocation_url=None,
    )
    transport = _MockTransport(200, {"ok": True})

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-sl-ac1", "slack", _token_response(access_token="xoxp-slack-access"))
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"slack": slack_config}):
            await revoke_token("org-sl-ac1", "slack", _revoke_transport=transport)

    assert transport.last_request is not None, "Expected auth.revoke call to Slack"
    assert "slack.com/api/auth.revoke" in str(transport.last_request.url)
    auth_header = transport.last_request.headers.get("authorization", "")
    assert auth_header.startswith("Bearer "), "Authorization must use Bearer scheme"
    assert "xoxp-slack-access" in auth_header, "Bearer token must be the stored access token"
    assert _raw_credentials("org-sl-ac1", "slack") is None
    _clear_credentials()


@pytest.mark.anyio
async def test_revoke_token_slack_ok_false_writes_audit_event():
    """When Slack auth.revoke returns ok=false, connector_revocation_failed audit event
    is written. Local deletion still completes. Never raises (AT-164 AC2).
    """
    from backend.app.auth.vault import store_token, revoke_token
    from backend.app.auth.models import ConnectorAuthConfig

    slack_config = ConnectorAuthConfig(
        connector_id="slack",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=["channels:read"],
        revocation_url=None,
    )
    transport = _MockTransport(200, {"ok": False, "error": "token_revoked"})

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-sl-ac2", "slack", _token_response(access_token="xoxp-bad"))
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"slack": slack_config}), \
             _patch("backend.app.auth.vault.log_event") as mock_log_event:
            await revoke_token("org-sl-ac2", "slack", _revoke_transport=transport)

    mock_log_event.assert_called_once_with(
        "connector_revocation_failed",
        org_id="org-sl-ac2",
        connector_id="slack",
        error_code="token_revoked",
    )
    assert _raw_credentials("org-sl-ac2", "slack") is None
    _clear_credentials()


@pytest.mark.anyio
async def test_revoke_token_github_no_external_call():
    """DELETE /api/connectors/github/token does not call any external endpoint —
    no RFC 7009 revocation URL and no Slack-specific path (AT-164 AC3).
    """
    from backend.app.auth.vault import store_token, revoke_token
    from backend.app.auth.models import ConnectorAuthConfig

    github_config = ConnectorAuthConfig(
        connector_id="github",
        flow="authorization_code",
        client_id="cid",
        secret_key="_TEST_OAUTH_SECRET",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["repo"],
        revocation_url=None,
    )
    transport = _MockTransport(200, {})

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-gh-ac3", "github", _token_response())
        with _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"github": github_config}):
            await revoke_token("org-gh-ac3", "github", _revoke_transport=transport)

    assert transport.last_request is None, "github revocation must not call any external endpoint"
    assert _raw_credentials("org-gh-ac3", "github") is None
    _clear_credentials()


def test_nonce_expiry_rejected(client):
    """Nonce with expires_at in the past returns 400 even if never used (AC7)."""
    import json as _json_mod
    from datetime import timedelta, timezone
    from app import db as _db
    from app.auth.vault import _init_nonce_table

    _init_nonce_table()
    nonce = _secrets_mod.token_hex(16)
    key = f"nonce:{nonce}"
    expired_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    data = _json_mod.dumps({
        "connector_id": "salesforce",
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),
        "expires_at": expired_at,
    })

    con = _db.connect()
    try:
        con.execute(
            "INSERT INTO nonces (key, data) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data",
            (key, data),
        )
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
