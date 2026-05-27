"""Contract tests for AT-73: auth data models (T1-S10-A).

AC9:  ConnectorAuthConfig has secret_key (env var name), never client_secret.
AC18: All three models importable from backend.app.auth and backend.app.auth.models.
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
