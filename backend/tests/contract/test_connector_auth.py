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
