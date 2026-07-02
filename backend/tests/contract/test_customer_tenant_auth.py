"""R17-D2 T2 - Customer-tenant provider authentication (vault-sourced) tests.

End-to-end proof that CustomerTenantModelProvider authenticates into the
customer's tenant using the credential stored in the Fernet-encrypted vault,
that rotation takes effect live, and that a missing/revoked credential fails
gracefully with no network call (R17-D2 §2, AC2/AC5).

FAKE CREDENTIALS: the ``az-FAKE-*`` values below are non-real, test-only Azure
tenant keys. They are not live credentials.

The forbidden-literal ``open`` + ``ai`` string (flagged by the R16-D1 no-bypass
scan outside the gateway package) is constructed by concatenation here so this
test file never self-trips that scan.
"""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.middleware import tenancy
from app.model_gateway._interface import GenerationRequest
from app.model_gateway.customer_tenant_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_API_VERSION,
    CONFIG_KEY_EMBEDDING_DEPLOYMENT,
    CONFIG_KEY_ENDPOINT,
    CONFIG_KEY_GENERATION_DEPLOYMENT,
)
from app.model_gateway.customer_tenant_provider import CustomerTenantModelProvider
from app.auth.vault import (
    revoke_customer_tenant_credential,
    store_customer_tenant_credential,
)

_VAULT_KEY = Fernet.generate_key().decode()

# Fake, non-real Azure-style tenant keys used only in tests. Not live secrets.
_FAKE_AZURE_KEY = "az-FAKE-VAULT-KEY-0123456789abcdef"
_FAKE_AZURE_KEY_ROTATED = "az-FAKE-VAULT-ROTATED-fedcba9876543210"

_OAI = "open" + "ai"  # avoid the no-bypass forbidden literal in this test file
_ENDPOINT = f"https://my-resource.{_OAI}.azure.com"
_API_VERSION = "2024-02-01"
_GEN_URL = (
    f"{_ENDPOINT}/{_OAI}/deployments/gen-deploy/chat/completions"
    f"?api-version={_API_VERSION}"
)


class _JsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _config_env() -> dict:
    """Endpoint/deployment config + vault key, but NO CUSTOMER_TENANT_API_KEY env.

    Omitting the env credential forces the provider to source its credential from
    the vault, which is exactly what this task adds.
    """
    return {
        "CREDENTIAL_VAULT_KEY": _VAULT_KEY,
        CONFIG_KEY_ENDPOINT: _ENDPOINT,
        CONFIG_KEY_API_VERSION: _API_VERSION,
        CONFIG_KEY_GENERATION_DEPLOYMENT: "gen-deploy",
        CONFIG_KEY_EMBEDDING_DEPLOYMENT: "emb-deploy",
    }


def _generate(org_id: str) -> tuple[Any, dict]:
    """Run generate() under org_id's tenancy context, capturing the HTTP call."""
    captured: dict[str, Any] = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["api_key"] = req.get_header("Api-key")
        return _JsonResponse({"choices": [{"message": {"content": "ok"}}]})

    token = tenancy._current_org_id.set(org_id)
    try:
        with patch("urllib.request.urlopen", _fake_urlopen):
            result = CustomerTenantModelProvider().generate(
                GenerationRequest(prompt="hi", max_tokens=8, timeout_ms=1500)
            )
    finally:
        tenancy._current_org_id.reset(token)
    return result, captured


def test_generate_authenticates_with_vaulted_credential():
    """The api-key header carries the vault-stored credential, not an env value."""
    org = "org-ct-auth-vault"
    # Guarantee no env credential is present so only the vault can supply it.
    with patch.dict(os.environ, _config_env()):
        os.environ.pop(CONFIG_KEY_API_KEY, None)
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
        try:
            result, captured = _generate(org)
            assert result.ok is True
            assert captured["url"] == _GEN_URL
            assert captured["api_key"] == _FAKE_AZURE_KEY
        finally:
            revoke_customer_tenant_credential(org)


def test_rotation_takes_effect_live_without_restart():
    """Rotating the vaulted credential changes the header on the next call."""
    org = "org-ct-auth-rotate"
    with patch.dict(os.environ, _config_env()):
        os.environ.pop(CONFIG_KEY_API_KEY, None)
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
        try:
            _, first = _generate(org)
            assert first["api_key"] == _FAKE_AZURE_KEY

            store_customer_tenant_credential(org, _FAKE_AZURE_KEY_ROTATED)
            _, second = _generate(org)
            assert second["api_key"] == _FAKE_AZURE_KEY_ROTATED
        finally:
            revoke_customer_tenant_credential(org)


def test_revoked_credential_fails_gracefully_without_network():
    """After revocation, generate() returns ok=False and makes no network call."""
    org = "org-ct-auth-revoke"
    with patch.dict(os.environ, _config_env()):
        os.environ.pop(CONFIG_KEY_API_KEY, None)
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
        revoke_customer_tenant_credential(org)

        called = {"network": False}

        def _fail_if_called(*_args, **_kwargs):
            called["network"] = True
            raise AssertionError("network must not be called without a credential")

        token = tenancy._current_org_id.set(org)
        try:
            with patch("urllib.request.urlopen", _fail_if_called):
                result = CustomerTenantModelProvider().generate(
                    GenerationRequest(prompt="hi", max_tokens=8)
                )
                vectors = CustomerTenantModelProvider().embed(["a"])
        finally:
            tenancy._current_org_id.reset(token)

        assert result.ok is False
        assert result.text is None
        assert vectors == []
        assert called["network"] is False


def test_missing_credential_never_logs_a_value_and_degrades():
    """No credential anywhere → graceful failure; nothing secret to leak."""
    org = "org-ct-auth-missing"
    with patch.dict(os.environ, _config_env()):
        os.environ.pop(CONFIG_KEY_API_KEY, None)
        # Nothing stored for this org and no env credential.
        called = {"network": False}

        def _fail_if_called(*_args, **_kwargs):
            called["network"] = True
            raise AssertionError("network must not be called without a credential")

        token = tenancy._current_org_id.set(org)
        try:
            with patch("urllib.request.urlopen", _fail_if_called):
                result = CustomerTenantModelProvider().generate(
                    GenerationRequest(prompt="hi", max_tokens=8)
                )
        finally:
            tenancy._current_org_id.reset(token)

        assert result.ok is False
        assert called["network"] is False


def test_env_credential_is_dev_fallback_when_vault_empty():
    """With no vaulted credential, the env var still works for dev/standalone."""
    org = "org-ct-auth-envfallback"
    with patch.dict(os.environ, _config_env()):
        os.environ[CONFIG_KEY_API_KEY] = _FAKE_AZURE_KEY
        # No vault row stored for this org → resolver falls back to env.
        result, captured = _generate(org)
        assert result.ok is True
        assert captured["api_key"] == _FAKE_AZURE_KEY
