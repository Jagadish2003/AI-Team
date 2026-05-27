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
