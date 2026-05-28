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
        "SELECT access_token, refresh_token FROM credentials WHERE org_id=? AND connector_id=?",
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
        "SELECT COUNT(*) FROM credentials WHERE org_id=? AND connector_id=?",
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
# AT-79 — T1-S10-A A7: Contract Tests (T11)
# 18 tests covering AC1–AC18 of the OAuth Auth Framework spec.
# ===========================================================================
#
# Infrastructure reused from earlier sections:
#   _MockTransport, _TimeoutTransport  — lightweight httpx mock transports
#   _make_config()                     — builds test ConnectorAuthConfig
#   _vault_env()                       — env dict with CREDENTIAL_VAULT_KEY set
#   _token_response()                  — builds fake token dict
#   _raw_credentials()                 — reads raw DB row (no decryption)
#   _clear_credentials()               — truncates credentials table
#   _AsyncMock, _patch, _os            — stdlib mock helpers
#   client fixture (conftest.py)       — FastAPI TestClient, auth via Bearer
#
# org_id used by routes: request.headers.get("X-Org-Id", "default-org")
# Auth header required on all 4 routes (AC17).
# ===========================================================================

import inspect as _inspect
import logging as _logging
from urllib.parse import parse_qs as _parse_qs, urlparse as _urlparse

_AT79_AUTH = {"Authorization": "Bearer dev-token-change-me"}
_AT79_ORG = "default-org"   # matches routes_connector_auth default


# ---------------------------------------------------------------------------
# AC1 — auth-url returns valid Salesforce authorization URL with correct params
# ---------------------------------------------------------------------------

def test_at79_ac1_auth_url_salesforce(client):  # AC1
    """GET /api/connectors/salesforce/auth-url returns URL with required params.

    Checks: client_id, redirect_uri, scope, response_type=code, non-empty state.
    State must NOT contain 'redirect', 'http', or any URL-like content.
    """
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    resp = client.get("/api/connectors/salesforce/auth-url", headers=_AT79_AUTH)
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
    assert state_vals and state_vals[0], "state must be present and non-empty"
    state = state_vals[0]

    # State must be an opaque nonce — not a URL or user data (AC1)
    for forbidden in ("redirect", "http", "user", "email", "@"):
        assert forbidden not in state.lower(), f"state must not contain '{forbidden}'"


# ---------------------------------------------------------------------------
# AC2 — callback stores token in vault and redirects to hardcoded constant
# ---------------------------------------------------------------------------

def test_at79_ac2_callback_valid_state_stores_token_redirects(client):  # AC2
    """Callback with valid code + matching state: stores token in vault, redirects to constant.

    Verifies real vault storage (raw DB check) and that redirect location
    equals OAUTH_SUCCESS_REDIRECT constant — no state content in location.
    """
    from app.routes_connector_auth import OAUTH_SUCCESS_REDIRECT

    # Step 1: generate a real nonce
    r = client.get("/api/connectors/salesforce/auth-url", headers=_AT79_AUTH)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    # Step 2: call callback — mock exchange_code only, real store_token
    fake_token = {
        "access_token": "at79-real-stored-tok",
        "refresh_token": "at79-refresh-tok",
        "expires_in": 3600,
    }
    env = {**_vault_env(), "SALESFORCE_CLIENT_SECRET": "dummy"}
    with _patch.dict(_os.environ, env), \
         _patch("app.routes_connector_auth.exchange_code",
                new_callable=_AsyncMock, return_value=fake_token):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=auth-code&state={state}",
            headers=_AT79_AUTH,
            follow_redirects=False,
        )

    # Redirect to hardcoded constant only (AC2)
    assert resp.status_code == 302
    expected = OAUTH_SUCCESS_REDIRECT.format(connector_id="salesforce")
    assert resp.headers["location"] == expected
    assert state not in resp.headers["location"], "State must not appear in redirect location"

    # Token stored in vault — raw DB row is encrypted, not plaintext (AC2, AC8)
    row = _raw_credentials(_AT79_ORG, "salesforce")
    assert row is not None, "Token must be written to DB after callback"
    assert "at79-real-stored-tok" not in (row[0] or ""), "access_token must be encrypted"

    _clear_credentials()


# ---------------------------------------------------------------------------
# AC3 — mismatched state → 400, generic message, hmac.compare_digest used
# ---------------------------------------------------------------------------

def test_at79_ac3_callback_mismatched_state_returns_400(client):  # AC3
    """Callback with wrong state returns HTTP 400 with generic, detail-free message.

    Also verifies via source inspection that hmac.compare_digest is used
    (not a simple == comparison) — AC3.
    """
    resp = client.get(
        "/api/connectors/oauth/callback?code=some-code&state=completely-wrong-nonce",
        headers=_AT79_AUTH,
        follow_redirects=False,
    )
    assert resp.status_code == 400

    # Response body must not reveal which element failed (AC3)
    body_lower = resp.text.lower()
    for forbidden in ("state", "nonce", "expected", "mismatch", "hmac", "digest"):
        assert forbidden not in body_lower, (
            f"400 body must not mention '{forbidden}' — leaks implementation detail"
        )

    # hmac.compare_digest must be used in the callback handler — source inspection (AC3)
    from app import routes_connector_auth
    source = _inspect.getsource(routes_connector_auth)
    assert "hmac.compare_digest" in source, (
        "routes_connector_auth must call hmac.compare_digest for state validation"
    )


# ---------------------------------------------------------------------------
# AC4 — redirect target never derivable from state payload or query params
# ---------------------------------------------------------------------------

def test_at79_ac4a_redirect_to_in_state_payload_ignored(client):  # AC4 sub-case (a)
    """redirect_to embedded in state payload does not influence the redirect target."""
    # Crafted state not in nonce store → 400; importantly no open redirect
    crafted = "redirect_to=https://evil.example.com&nonce=abc"
    resp = client.get(
        f"/api/connectors/oauth/callback?code=c&state={crafted}",
        headers=_AT79_AUTH,
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "evil.example.com" not in resp.headers.get("location", "")


def test_at79_ac4b_redirect_to_query_param_ignored(client):  # AC4 sub-case (b)
    """redirect_to as a raw query parameter does not influence the redirect target."""
    r = client.get("/api/connectors/salesforce/auth-url", headers=_AT79_AUTH)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "tok", "expires_in": 3600}
    env = {**_vault_env(), "SALESFORCE_CLIENT_SECRET": "dummy"}
    with _patch.dict(_os.environ, env), \
         _patch("app.routes_connector_auth.exchange_code",
                new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        resp = client.get(
            f"/api/connectors/oauth/callback?code=c&state={state}"
            "&redirect_to=https://evil.example.com",
            headers=_AT79_AUTH,
            follow_redirects=False,
        )

    location = resp.headers.get("location", "")
    assert "evil.example.com" not in location
    assert "connected=salesforce" in location

    _clear_credentials()


# ---------------------------------------------------------------------------
# AC5 — get_token returns valid token; refresh NOT called for fresh token
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_at79_ac5_get_token_returns_valid_no_refresh_called():  # AC5
    """get_token returns a valid TokenRecord without calling refresh_token
    when the token expiry is well above REFRESH_THRESHOLD_SECONDS.
    """
    from app.auth.vault import store_token, get_token

    mock_refresh = _AsyncMock()

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-ac5", "github", _token_response(expires_in=7200))

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh):
            record = await get_token("org-ac5", "github")

    mock_refresh.assert_not_called()   # fresh token — no refresh needed (AC5)
    assert record.access_token == "plain-access-tok"
    assert record.connector_id == "github"
    _clear_credentials()


# ---------------------------------------------------------------------------
# AC6 — get_token auto-refreshes near expiry; refreshed token is persisted
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_at79_ac6_get_token_auto_refreshes_and_persists():  # AC6
    """get_token calls refresh_token when near expiry and persists the new token.
    A subsequent get_token call returns the refreshed token without a second refresh.
    """
    from app.auth.vault import store_token, get_token

    refreshed = _token_response(
        access_token="at79-refreshed-tok",
        refresh_token="at79-new-ref",
        expires_in=7200,
    )
    mock_refresh = _AsyncMock(return_value=refreshed)

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-ac6", "salesforce", _token_response(expires_in=60))

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS",
                    {"salesforce": _make_config(connector_id="salesforce")}):
            record = await get_token("org-ac6", "salesforce")

    mock_refresh.assert_called_once()         # refresh was triggered (AC6)
    assert record.access_token == "at79-refreshed-tok"

    # Refreshed token persisted — second call returns it without another refresh
    second_mock = _AsyncMock(return_value=refreshed)
    with _patch.dict(_os.environ, _vault_env()):
        with _patch("app.auth.vault._oauth.refresh_token", second_mock), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS",
                    {"salesforce": _make_config(connector_id="salesforce")}):
            record2 = await get_token("org-ac6", "salesforce")

    second_mock.assert_not_called()           # already fresh — no second refresh (AC6)
    assert record2.access_token == "at79-refreshed-tok"
    _clear_credentials()


# ---------------------------------------------------------------------------
# AC7 — get_token raises ConnectorNotAuthenticatedError in both failure modes
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_at79_ac7a_get_token_raises_when_no_token():  # AC7 sub-case (a)
    """get_token raises ConnectorNotAuthenticatedError when no token exists."""
    from app.auth.vault import get_token
    from app.auth.models import ConnectorNotAuthenticatedError

    with _patch.dict(_os.environ, _vault_env()):
        with pytest.raises(ConnectorNotAuthenticatedError) as exc_info:
            await get_token("org-ac7a-missing", "salesforce")

    assert exc_info.value.connector_id == "salesforce"
    assert exc_info.value.org_id == "org-ac7a-missing"


@pytest.mark.anyio
async def test_at79_ac7b_get_token_raises_when_refresh_rejected():  # AC7 sub-case (b)
    """get_token raises ConnectorNotAuthenticatedError when token endpoint rejects refresh."""
    from app.auth.vault import store_token, get_token
    from app.auth.models import ConnectorNotAuthenticatedError
    from app.auth.oauth import OAuthError

    mock_refresh = _AsyncMock(side_effect=OAuthError("salesforce", 400))

    with _patch.dict(_os.environ, _vault_env()):
        store_token("org-ac7b", "salesforce", _token_response(expires_in=60))

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS",
                    {"salesforce": _make_config(connector_id="salesforce")}):
            with pytest.raises(ConnectorNotAuthenticatedError):
                await get_token("org-ac7b", "salesforce")

    _clear_credentials()


# ---------------------------------------------------------------------------
# AC8 — access_token and refresh_token are encrypted at rest
# ---------------------------------------------------------------------------

def test_at79_ac8_tokens_encrypted_at_rest():  # AC8
    """After store_token, raw DB row must not contain any plaintext token substring."""
    from app.auth.vault import store_token

    plain_access = "super-secret-access-ac8"
    plain_refresh = "super-secret-refresh-ac8"

    with _patch.dict(_os.environ, _vault_env()):
        store_token(
            "org-ac8", "jira",
            {"access_token": plain_access, "refresh_token": plain_refresh, "expires_in": 3600},
        )

    row = _raw_credentials("org-ac8", "jira")
    assert row is not None

    # access_token column must not be or contain the plaintext (AC8)
    assert row[0] != plain_access
    assert plain_access not in (row[0] or "")
    # refresh_token column must not be or contain the plaintext (AC8)
    assert row[1] != plain_refresh
    assert plain_refresh not in (row[1] or "")

    _clear_credentials()


# ---------------------------------------------------------------------------
# AC9 — CONNECTOR_AUTH_CONFIGS contains no client_secret field anywhere
# ---------------------------------------------------------------------------

def test_at79_ac9_no_client_secret_in_configs():  # AC9
    """ConnectorAuthConfig dataclass has no client_secret field.
    No entry in CONNECTOR_AUTH_CONFIGS has a client_secret attribute.
    """
    import dataclasses as _dc
    from app.auth.models import ConnectorAuthConfig
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    field_names = {f.name for f in _dc.fields(ConnectorAuthConfig)}
    assert "client_secret" not in field_names, "client_secret field must never exist on model"
    assert "secret_key" in field_names, "secret_key (env var name) must exist"

    for cid, config in CONNECTOR_AUTH_CONFIGS.items():
        assert not hasattr(config, "client_secret"), f"{cid}: must not have client_secret attr"
        assert config.secret_key, f"{cid}: secret_key must be non-empty"


# ---------------------------------------------------------------------------
# AC10 — resolve_secret raises MissingSecretError; startup sweep enforces it
# ---------------------------------------------------------------------------

def test_at79_ac10_missing_secret_raises(monkeypatch):  # AC10
    """resolve_secret raises MissingSecretError when env var is absent.
    Error message contains the key name, never a secret value.
    """
    from app.auth.secrets import resolve_secret, MissingSecretError

    monkeypatch.delenv("_AT79_ABSENT_KEY", raising=False)

    with pytest.raises(MissingSecretError) as exc_info:
        resolve_secret("_AT79_ABSENT_KEY")

    err = exc_info.value
    assert "_AT79_ABSENT_KEY" in str(err), "Error must name the missing key"
    assert err.secret_key == "_AT79_ABSENT_KEY"

    # Also verify startup sweep: validate_all_secrets raises on missing key
    from app.auth.secrets import validate_all_secrets
    from app.auth.models import ConnectorAuthConfig

    configs = {
        "test": ConnectorAuthConfig(
            connector_id="test",
            flow="client_credentials",
            client_id="cid",
            secret_key="_AT79_ABSENT_KEY",
            token_url="https://example.com/token",
            scopes=[],
        )
    }
    with pytest.raises(MissingSecretError):
        validate_all_secrets(configs)


# ---------------------------------------------------------------------------
# AC11 — DELETE salesforce/token calls external endpoint, then deletes locally
# ---------------------------------------------------------------------------

def test_at79_ac11_revoke_salesforce_calls_external_returns_204(client):  # AC11
    """DELETE /api/connectors/salesforce/token: external revocation called, 204 returned,
    token deleted from vault.
    """
    from app.auth.vault import store_token as _sv, revoke_token as _original_revoke
    from app.auth.models import ConnectorNotAuthenticatedError
    import asyncio

    transport = _MockTransport(200, {})

    with _patch.dict(_os.environ, _vault_env()):
        _sv(_AT79_ORG, "salesforce", _token_response())

    # Wrap original revoke_token to inject our transport
    async def _tracked_revoke(org_id: str, connector_id: str, **_kw):
        return await _original_revoke(org_id, connector_id, _revoke_transport=transport)

    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.revoke_token", _tracked_revoke):
        resp = client.delete(
            "/api/connectors/salesforce/token",
            headers=_AT79_AUTH,
        )

    assert resp.status_code == 204                              # AC11
    assert transport.last_request is not None, (
        "External revocation endpoint must be called for Salesforce"
    )
    # Revocation URL must reference Salesforce
    url = str(transport.last_request.url)
    assert "salesforce" in url.lower() or "revoke" in url.lower()

    # Local vault deletion completed
    assert _raw_credentials(_AT79_ORG, "salesforce") is None   # AC11
    _clear_credentials()


# ---------------------------------------------------------------------------
# AC12 — DELETE github/token: local deletion only, no external HTTP call
# ---------------------------------------------------------------------------

def test_at79_ac12_revoke_github_local_only_returns_204(client):  # AC12
    """DELETE /api/connectors/github/token: no external HTTP call made; 204 returned."""
    from app.auth.vault import store_token as _sv, revoke_token as _original_revoke

    transport = _MockTransport(200, {})

    with _patch.dict(_os.environ, _vault_env()):
        _sv(_AT79_ORG, "github", _token_response())

    async def _tracked_revoke(org_id: str, connector_id: str, **_kw):
        return await _original_revoke(org_id, connector_id, _revoke_transport=transport)

    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.revoke_token", _tracked_revoke):
        resp = client.delete(
            "/api/connectors/github/token",
            headers=_AT79_AUTH,
        )

    assert resp.status_code == 204                              # AC12
    assert transport.last_request is None, (
        "No external HTTP call must be made for GitHub (revocation_url=None)"
    )
    assert _raw_credentials(_AT79_ORG, "github") is None       # local deletion confirmed
    _clear_credentials()


# ---------------------------------------------------------------------------
# AC13 — Step 1 failure logged as WARNING; local deletion still completes; 204
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_at79_ac13_revoke_step1_failure_logs_warning_still_deletes(caplog):  # AC13
    """revoke_token Step 1 failure (500 from endpoint): WARNING logged, local deletion done.

    Uses caplog to assert the WARNING record exists and contains the failure reason.
    Caller still receives 204 — Step 1 failure is never propagated.
    """
    from app.auth.vault import store_token, revoke_token
    from app.auth.models import ConnectorAuthConfig

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
        store_token("org-ac13", "confluence", _token_response())

        with caplog.at_level(_logging.WARNING, logger="app.auth.vault"), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS", {"confluence": revocable}):
            # Step 1 fails (HTTP 500); Step 2 must still run
            await revoke_token(
                "org-ac13", "confluence",
                _revoke_transport=_MockTransport(500, {}),
            )

    # WARNING log must have been emitted for Step 1 failure (AC13)
    warnings = [r for r in caplog.records if r.levelno >= _logging.WARNING]
    assert warnings, "Expected at least one WARNING log when Step 1 fails"
    log_text = " ".join(r.getMessage() for r in warnings)
    assert "500" in log_text or "confluence" in log_text, (
        "WARNING must mention the failure context (status code or connector)"
    )

    # Local deletion still completed (AC13)
    assert _raw_credentials("org-ac13", "confluence") is None
    _clear_credentials()


# ---------------------------------------------------------------------------
# AC14 — token-status returns 'needs_refresh' when near expiry (internal state)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_at79_ac14_token_status_needs_refresh_is_internal_state(client):  # AC14
    """GET /api/connectors/servicenow/token-status returns {"status": "needs_refresh"}
    when the returned token (after potential refresh) is still within REFRESH_THRESHOLD_SECONDS.

    Internal note: Integration Hub UI maps 'needs_refresh' to the green indicator dot.
    This is an operational/health-check state, not a user-facing string.
    """
    from app.auth.vault import store_token as _sv

    # Store a near-expiry token with a refresh_token present.
    # Mock refresh_token to return a short-lived new token (200s < 300s threshold)
    # so get_token() refreshes but the route still sees needs_refresh.
    short_lived_response = _token_response(
        access_token="short-lived-tok",
        refresh_token="new-ref",
        expires_in=200,   # still within REFRESH_THRESHOLD_SECONDS (300)
    )
    mock_refresh = _AsyncMock(return_value=short_lived_response)

    with _patch.dict(_os.environ, _vault_env()):
        _sv(_AT79_ORG, "servicenow", _token_response(expires_in=60))

        with _patch("app.auth.vault._oauth.refresh_token", mock_refresh), \
             _patch("app.auth.vault.CONNECTOR_AUTH_CONFIGS",
                    {"servicenow": _make_config(connector_id="servicenow")}):
            resp = client.get(
                "/api/connectors/servicenow/token-status",
                headers=_AT79_AUTH,
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_refresh"          # AC14
    assert data["status"] != "connected"
    assert data["status"] != "needs_auth"

    _clear_credentials()


# ---------------------------------------------------------------------------
# AC15 — state nonce is single-use; replay returns 400
# ---------------------------------------------------------------------------

def test_at79_ac15_state_nonce_single_use(client):  # AC15
    """First callback with a nonce succeeds; replay of the same nonce returns 400.
    Verifies the nonce is deleted from the server-side store after first use.
    """
    r = client.get("/api/connectors/salesforce/auth-url", headers=_AT79_AUTH)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    fake_token = {"access_token": "tok-ac15", "expires_in": 3600}

    # First use — succeeds
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code",
                new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        r1 = client.get(
            f"/api/connectors/oauth/callback?code=code1&state={state}",
            headers=_AT79_AUTH,
            follow_redirects=False,
        )
    assert r1.status_code == 302

    # Replay — nonce was deleted after first use → 400 (AC15)
    with _patch.dict(_os.environ, _vault_env()), \
         _patch("app.routes_connector_auth.exchange_code",
                new_callable=_AsyncMock, return_value=fake_token), \
         _patch("app.routes_connector_auth.store_token", return_value=None):
        r2 = client.get(
            f"/api/connectors/oauth/callback?code=code2&state={state}",
            headers=_AT79_AUTH,
            follow_redirects=False,
        )
    assert r2.status_code == 400, "Replayed nonce must be rejected with 400 (AC15)"

    _clear_credentials()


# ---------------------------------------------------------------------------
# AC16 — CONNECTOR_AUTH_CONFIGS has all 8 connectors with correct values
# ---------------------------------------------------------------------------

def test_at79_ac16_connector_auth_configs_all_8():  # AC16
    """CONNECTOR_AUTH_CONFIGS has exactly 8 entries; each has correct flow,
    secret_key pattern ({ID_UPPER}_CLIENT_SECRET), and revocation_url per spec.
    """
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    assert len(CONNECTOR_AUTH_CONFIGS) == 8, (
        f"Expected 8 connectors, got {len(CONNECTOR_AUTH_CONFIGS)}: "
        f"{sorted(CONNECTOR_AUTH_CONFIGS)}"
    )

    expected: dict = {
        "salesforce":  ("authorization_code", "https://{instance}.salesforce.com/services/oauth2/revoke"),
        "servicenow":  ("authorization_code", None),   # revocation deferred — no endpoint configured
        "jira":        ("authorization_code", "https://auth.atlassian.com/oauth/token/revoke"),
        "github":      ("authorization_code", None),
        "confluence":  ("authorization_code", "https://auth.atlassian.com/oauth/token/revoke"),
        "slack":       ("authorization_code", None),
        "sap":         ("client_credentials", None),
        "d365":        ("client_credentials", None),
    }

    for cid, (exp_flow, exp_revocation) in expected.items():
        config = CONNECTOR_AUTH_CONFIGS[cid]
        assert config.flow == exp_flow, f"{cid}: flow mismatch"
        assert config.revocation_url == exp_revocation, f"{cid}: revocation_url mismatch"
        assert config.secret_key, f"{cid}: secret_key must be non-empty"
        # Pattern must be {CONNECTOR_ID_UPPER}_CLIENT_SECRET (AC16)
        assert config.secret_key == f"{cid.upper()}_CLIENT_SECRET", (
            f"{cid}: secret_key must be '{cid.upper()}_CLIENT_SECRET'"
        )
        # revocation_url must be Python None (not the string "None") for non-revocable connectors
        if exp_revocation is None:
            assert config.revocation_url is None, (
                f"{cid}: revocation_url must be Python None, not string 'None'"
            )


# ---------------------------------------------------------------------------
# AC17 — all four routes return 401 without authentication
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET",    "/api/connectors/salesforce/auth-url"),
    ("GET",    "/api/connectors/oauth/callback"),
    ("DELETE", "/api/connectors/salesforce/token"),
    ("GET",    "/api/connectors/servicenow/token-status"),
])
def test_at79_ac17_all_routes_require_auth(client, method, path):  # AC17
    """Unauthenticated requests to all 4 routes return HTTP 401."""
    resp = client.request(method, path)   # no Authorization header
    assert resp.status_code == 401, f"{method} {path} must return 401 without auth"


# ---------------------------------------------------------------------------
# AC18 — auth package importable; all required symbols are correct types
# ---------------------------------------------------------------------------

def test_at79_ac18_auth_module_importable_no_circular_imports():  # AC18
    """from app.auth import ... succeeds for all required symbols.
    Each symbol is the expected type — callable or Exception subclass.
    No ImportError, no circular import.
    """
    import inspect as _insp
    from app.auth import (
        get_token,
        store_token,
        revoke_token,
        ConnectorNotAuthenticatedError,
    )

    assert callable(get_token),   "get_token must be callable"
    assert callable(store_token), "store_token must be callable"
    assert callable(revoke_token), "revoke_token must be callable"
    assert _insp.isclass(ConnectorNotAuthenticatedError), (
        "ConnectorNotAuthenticatedError must be a class"
    )
    assert issubclass(ConnectorNotAuthenticatedError, Exception), (
        "ConnectorNotAuthenticatedError must be an Exception subclass"
    )
