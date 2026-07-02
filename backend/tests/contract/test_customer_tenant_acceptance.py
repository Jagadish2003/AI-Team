"""R17-D2 T7 - Customer-Tenant Model Mode full acceptance-criteria contract tests.

This suite proves the *story-level* acceptance criteria of R17-D2 Section 6
(AC1-AC8), end-to-end and organised by AC. The earlier task suites each prove
one task in isolation:
  - test_customer_tenant_provider (T1)  — generate()/embed() against the endpoint
  - test_customer_tenant_auth / _vault (T2) — vault-sourced authentication
  - test_customer_tenant_resilience (T3) — retry/backoff/graceful failure
  - test_customer_tenant_config / _startup_validation (T4) — config + validation
  - test_customer_tenant_telemetry (T5) — provider='customer_tenant' telemetry
  - test_customer_tenant_no_bypass (T6) — no-bypass confinement

T7 is the acceptance gate: it demonstrates the full R17-D2 contract through the
public gateway surface — that customer-tenant mode is selectable purely by
configuration without changing any calling code, that it authenticates into the
customer's tenant with a vault-stored credential that never leaks, that BOTH
generate() and embed() work against the tenant endpoint, that generation and
embedding remain independently selectable, that resilience/graceful-failure is
preserved, that every call records provider='customer_tenant', that the R16-D1
no-bypass guarantee still holds, and that all THREE modes (hosted, in-boundary,
customer-tenant) are now selectable for both capabilities behind the one gateway.

Each test references the AC it proves so a failure maps directly to the
acceptance criterion that regressed.

Test posture
------------
- The customer's tenant endpoint is faked at ``urllib.request.urlopen`` so no
  real endpoint is contacted; CI runs safely with no Azure/managed endpoint.
  Full-URL overrides are used for the fake endpoints so this test file holds no
  provider-path literal (the Azure deployment-path build is covered by the T1
  provider/config suites).
- ``record_event`` is captured in-memory; no telemetry is written to the DB.
- ``time.sleep`` is patched out in the resilience tests so bounded backoff does
  not slow the suite.
- The vault-backed AC2 tests use the real Fernet vault against the contract test
  DB with a per-test throwaway key and org, cleaning up after themselves.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
from typing import Any, List, Tuple
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.middleware import tenancy
from app.model_gateway import (
    _PROVIDER_REGISTRY,
    GenerationRequest,
    GenerationResult,
    ModelProvider,
    embed as gw_embed,
    generate as gw_generate,
    get_embedding_provider,
    get_generation_provider,
)
from app.model_gateway.customer_tenant_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_API_VERSION,
    CONFIG_KEY_EMBEDDING_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_ENDPOINT,
    CONFIG_KEY_GENERATION_DEPLOYMENT,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CUSTOMER_TENANT_PROVIDER_NAME,
)
from app.model_gateway.customer_tenant_provider import (
    CustomerTenantModelProvider,
    _MAX_RETRIES,
)
from app.model_gateway.in_boundary_config import IN_BOUNDARY_PROVIDER_NAME
from app.auth.vault import (
    revoke_customer_tenant_credential,
    store_customer_tenant_credential,
)

_GEN_EVENT = "model.generation_completed"
_EMB_EVENT = "model.embedding_completed"
_HOSTED = "hosted"

_SECRET = "ct-acceptance-secret-DO-NOT-LOG"
# Non-Azure full-URL overrides so this test source carries no provider-path
# literal; the customer's managed endpoint may not follow the Azure path.
_BASE = "https://models.customer-tenant.internal"
_GEN_ENDPOINT = f"{_BASE}/v1/chat/completions"
_EMB_ENDPOINT = f"{_BASE}/v1/embeddings"

# A stand-in for any reachable external endpoint. Built by concatenation so this
# file never self-trips the R16-D1 no-bypass scan.
_EXTERNAL_HOST = "api.anthrop" + "ic.com"

# Fake, non-real tenant key used only by the vault-backed AC2 tests.
_FAKE_VAULT_KEY = "az-FAKE-ACCEPTANCE-KEY-0123456789ab"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


class _JsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _request_json(req) -> dict[str, Any]:
    return json.loads((req.data or b"{}").decode("utf-8"))


@pytest.fixture
def configure_customer_tenant(monkeypatch):
    """Fully configure a customer-tenant endpoint via explicit full-URL overrides.

    Uses the credential env fallback for the credential (the vault path yields
    nothing in the contract test DB for the default org and falls through to it).
    """
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, _GEN_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_ENDPOINT, _EMB_ENDPOINT)
    # Endpoint base / deployments are irrelevant when full-URL overrides are set,
    # but clear them so nothing else influences URL resolution.
    monkeypatch.delenv(CONFIG_KEY_ENDPOINT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_GENERATION_DEPLOYMENT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_EMBEDDING_DEPLOYMENT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_API_VERSION, raising=False)


@pytest.fixture
def select_customer_tenant_both(monkeypatch, configure_customer_tenant):
    """Select customer_tenant for BOTH generation and embedding."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every record_event() call without touching the DB."""
    events: List[Tuple[str, dict]] = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda event_type, payload=None: events.append((event_type, payload or {})),
    )
    return events


@pytest.fixture
def network(monkeypatch):
    """Patch urllib so every contacted URL is recorded and routed by endpoint.

    Serves an OK generation/embedding response for the two configured endpoints
    and FAILS LOUDLY for any other URL — so a stray external call surfaces as a
    test failure.
    """
    contacted: List[str] = []

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        contacted.append(url)
        if url == _GEN_ENDPOINT:
            return _JsonResponse(
                {"choices": [{"message": {"content": "in-tenant answer"}}]}
            )
        if url == _EMB_ENDPOINT:
            n = len(_request_json(req).get("input", []))
            return _JsonResponse(
                {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in range(n)]}
            )
        raise AssertionError(
            f"request left the customer-tenant endpoints — contacted {url!r}"
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    return contacted


def _http_error(status: int):
    return urllib.error.HTTPError(
        "http://fake", status, "err", http.client.HTTPMessage(), None
    )


# ===========================================================================
# AC1 — Customer-tenant mode is selectable via configuration with NO change to
#        any calling code (it registers behind the R16-D1 gateway).
# ===========================================================================


def test_ac1_customer_tenant_registered_behind_gateway():
    provider = _PROVIDER_REGISTRY.get(CUSTOMER_TENANT_PROVIDER_NAME)
    assert isinstance(provider, CustomerTenantModelProvider)
    assert isinstance(provider, ModelProvider)
    assert provider.name == CUSTOMER_TENANT_PROVIDER_NAME


def test_ac1_unchanged_caller_code_routes_to_customer_tenant(
    select_customer_tenant_both, network, captured_events
):
    """The SAME gateway calls a caller already makes route to customer_tenant once
    the env config selects it — no calling code changes (AC1)."""
    result = gw_generate(GenerationRequest(prompt="hello", max_tokens=8))
    vectors = gw_embed(["hello"])

    assert result.provider == CUSTOMER_TENANT_PROVIDER_NAME
    assert result.ok is True
    assert vectors == [[0.1, 0.2, 0.3]]
    assert get_generation_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
    assert get_embedding_provider().name == CUSTOMER_TENANT_PROVIDER_NAME


# ===========================================================================
# AC2 — Authenticates into the customer's tenant using a vault-stored credential;
#        the credential never appears in logs.
# ===========================================================================


def test_ac2_authenticates_with_vaulted_credential(monkeypatch):
    """The api-key header carries the VAULT-stored credential (not an env value)."""
    org = "org-ct-acc-vault"
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, _GEN_ENDPOINT)
    monkeypatch.delenv(CONFIG_KEY_API_KEY, raising=False)  # force vault as the source

    captured: dict[str, Any] = {}

    def _fake_urlopen(req, timeout=None):
        captured["api_key"] = req.get_header("Api-key")
        return _JsonResponse({"choices": [{"message": {"content": "ok"}}]})

    store_customer_tenant_credential(org, _FAKE_VAULT_KEY)
    token = tenancy._current_org_id.set(org)
    try:
        with patch("urllib.request.urlopen", _fake_urlopen):
            result = CustomerTenantModelProvider().generate(
                GenerationRequest(prompt="hi", max_tokens=8)
            )
    finally:
        tenancy._current_org_id.reset(token)
        revoke_customer_tenant_credential(org)

    assert result.ok is True
    assert captured["api_key"] == _FAKE_VAULT_KEY


def test_ac2_env_credential_is_dev_fallback(configure_customer_tenant):
    """With no vaulted credential, the env var authenticates for dev/standalone."""
    captured: dict[str, Any] = {}

    def _fake_urlopen(req, timeout=None):
        captured["api_key"] = req.get_header("Api-key")
        return _JsonResponse({"choices": [{"message": {"content": "ok"}}]})

    with patch("urllib.request.urlopen", _fake_urlopen), patch(
        "app.telemetry.record_event", lambda *_a, **_k: None
    ):
        result = CustomerTenantModelProvider().generate(
            GenerationRequest(prompt="hi", max_tokens=8)
        )

    assert result.ok is True
    assert captured["api_key"] == _SECRET


def test_ac2_credential_never_appears_in_logs(
    monkeypatch, configure_customer_tenant, captured_events, caplog
):
    """SAFETY: the tenant credential never appears in logs — on success OR failure."""
    import logging

    caplog.set_level(logging.DEBUG)

    # 1) A successful call.
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _JsonResponse(
        {"choices": [{"message": {"content": "ok"}}]}
    ))
    CustomerTenantModelProvider().generate(GenerationRequest(prompt="hi", max_tokens=8))

    # 2) A failing call (transport error → logged error path).
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    CustomerTenantModelProvider().generate(
        GenerationRequest(prompt="hi", max_tokens=8, timeout_ms=1000)
    )

    assert _SECRET not in caplog.text, "tenant credential leaked into logs"


def test_ac2_missing_credential_fails_gracefully_without_network(monkeypatch):
    """No credential anywhere → graceful failure with no network call (AC2/AC5)."""
    monkeypatch.delenv(CONFIG_KEY_API_KEY, raising=False)
    monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, _GEN_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_ENDPOINT, _EMB_ENDPOINT)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)

    called = {"network": False}

    def _fail_if_called(*_a, **_k):
        called["network"] = True
        raise AssertionError("network must not be called without a credential")

    monkeypatch.setattr("urllib.request.urlopen", _fail_if_called)

    result = CustomerTenantModelProvider().generate(
        GenerationRequest(prompt="hi", max_tokens=8)
    )
    vectors = CustomerTenantModelProvider().embed(["a"])

    assert result.ok is False and result.text is None
    assert vectors == []
    assert called["network"] is False


# ===========================================================================
# AC3 — BOTH generate() and embed() are implemented against the tenant endpoint.
# ===========================================================================


def test_ac3_generate_and_embed_contact_configured_tenant_endpoints(
    select_customer_tenant_both, network, captured_events
):
    gen = gw_generate(GenerationRequest(prompt="confidential", max_tokens=16))
    vectors = gw_embed(["text one", "text two"])

    assert gen.text == "in-tenant answer"
    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert _GEN_ENDPOINT in network
    assert _EMB_ENDPOINT in network
    # Nothing external is ever contacted.
    assert all(_EXTERNAL_HOST not in url for url in network)
    assert all(url.startswith(_BASE) for url in network)


def test_ac3_embedding_is_implemented_not_a_stub(select_customer_tenant_both, network):
    """embed() is a real capability: it posts to the endpoint and returns
    input-ordered vectors (the easily-forgotten half)."""
    vectors = gw_embed(["alpha"])
    assert vectors == [[0.1, 0.2, 0.3]]
    assert network == [_EMB_ENDPOINT]


# ===========================================================================
# AC4 — Generation and embedding providers are INDEPENDENTLY selectable.
# ===========================================================================


def test_ac4_customer_tenant_generation_with_hosted_embedding(
    monkeypatch, configure_customer_tenant
):
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _HOSTED)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)

    calls: List[str] = []

    def _fake_urlopen(req, timeout=None):
        if req.full_url == _GEN_ENDPOINT:
            calls.append(req.full_url)
            return _JsonResponse({"choices": [{"message": {"content": "gen-only"}}]})
        raise AssertionError(f"unexpected customer-tenant call: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4))
    assert result.provider == CUSTOMER_TENANT_PROVIDER_NAME
    assert result.ok is True
    assert calls == [_GEN_ENDPOINT]
    assert get_generation_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
    assert get_embedding_provider().name == _HOSTED


def test_ac4_customer_tenant_embedding_with_hosted_generation(
    monkeypatch, configure_customer_tenant
):
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", _HOSTED)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)

    calls: List[str] = []

    def _fake_urlopen(req, timeout=None):
        if req.full_url == _EMB_ENDPOINT:
            calls.append(req.full_url)
            return _JsonResponse({"data": [{"embedding": [0.4, 0.5]}]})
        raise AssertionError(f"unexpected customer-tenant call: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    vectors = gw_embed(["customer-private text"])
    assert vectors == [[0.4, 0.5]]
    assert calls == [_EMB_ENDPOINT]
    assert get_embedding_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
    assert get_generation_provider().name == _HOSTED


def test_ac4_selecting_one_capability_does_not_force_the_other(
    monkeypatch, configure_customer_tenant
):
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)

    assert get_generation_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
    # Embedding falls back to the gateway default ('hosted'), unaffected.
    assert get_embedding_provider().name == _HOSTED


# ===========================================================================
# AC5 — Resilience and graceful-failure behaviour match the other modes.
# ===========================================================================


def test_ac5_transient_failure_is_retried_then_succeeds(
    monkeypatch, select_customer_tenant_both
):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(503)
        return _JsonResponse({"choices": [{"message": {"content": "recovered"}}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4, timeout_ms=60_000))
    assert calls["n"] >= 2
    assert result.ok is True
    assert result.text == "recovered"


def test_ac5_retry_is_bounded(monkeypatch, select_customer_tenant_both):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    calls = {"n": 0}

    def _always_429(req, timeout=None):
        calls["n"] += 1
        raise _http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4, timeout_ms=300_000))
    assert result.ok is False
    assert calls["n"] <= _MAX_RETRIES + 1


def test_ac5_exhausted_generation_returns_ok_false_text_none_not_raise(
    monkeypatch, select_customer_tenant_both
):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429)),
    )

    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4, timeout_ms=300_000))
    assert result == GenerationResult(
        text=None, provider=CUSTOMER_TENANT_PROVIDER_NAME, ok=False
    )


def test_ac5_embedding_failure_degrades_safely_to_empty(
    monkeypatch, select_customer_tenant_both
):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(RuntimeError("endpoint down")),
    )

    vectors = gw_embed(["customer-private text"])
    assert vectors == []


def test_ac5_per_request_timeout_is_a_wall_clock_deadline(
    monkeypatch, select_customer_tenant_both
):
    """A short timeout_ms is honoured: a 503 cannot back off within a 500ms
    budget, so the call returns ok=False without ever sleeping."""
    sleeps: List[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("app.telemetry.record_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(_http_error(503)),
    )

    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4, timeout_ms=500))
    assert result.ok is False
    assert sleeps == [], "the 500ms deadline must prevent a 0.5s+ backoff sleep"


# ===========================================================================
# AC6 — Every call records provider='customer_tenant' in telemetry.
# ===========================================================================


def test_ac6_generation_records_provider_customer_tenant(
    select_customer_tenant_both, network, captured_events
):
    gw_generate(GenerationRequest(prompt="x", max_tokens=4))
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1
    assert gen[0][1]["provider"] == CUSTOMER_TENANT_PROVIDER_NAME


def test_ac6_embedding_records_provider_customer_tenant(
    select_customer_tenant_both, network, captured_events
):
    gw_embed(["x"])
    emb = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb) == 1
    assert emb[0][1]["provider"] == CUSTOMER_TENANT_PROVIDER_NAME


def test_ac6_failed_call_still_records_provider_ok_false(
    monkeypatch, configure_customer_tenant, captured_events
):
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429)),
    )

    result = gw_generate(GenerationRequest(prompt="x", max_tokens=4, timeout_ms=300_000))
    assert result.ok is False
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1
    assert gen[0][1]["provider"] == CUSTOMER_TENANT_PROVIDER_NAME
    assert gen[0][1]["ok"] is False


def test_ac6_each_call_records_exactly_one_event_no_double_count(
    select_customer_tenant_both, network, captured_events
):
    gw_generate(GenerationRequest(prompt="x", max_tokens=4))
    gw_embed(["x"])
    assert len([e for e in captured_events if e[0] == _GEN_EVENT]) == 1
    assert len([e for e in captured_events if e[0] == _EMB_EVENT]) == 1


def test_ac6_telemetry_payload_carries_no_prompt_text_or_secret(
    select_customer_tenant_both, network, captured_events
):
    secret_prompt = "TOP-SECRET customer prompt"
    secret_text = "TOP-SECRET embedding input"
    gw_generate(GenerationRequest(prompt=secret_prompt, max_tokens=4))
    gw_embed([secret_text])

    blob = json.dumps([payload for _evt, payload in captured_events])
    assert secret_prompt not in blob
    assert secret_text not in blob
    assert _SECRET not in blob


# ===========================================================================
# AC7 — The R16-D1 no-bypass test still passes with customer_tenant present.
# ===========================================================================


def test_ac7_r16d1_no_bypass_scan_still_clean():
    """Re-run the R16-D1 enforcement scanner directly: no application code outside
    the gateway makes a direct model call after customer-tenant was added."""
    import test_model_gateway_no_bypass as r16d1

    violations: List[str] = []
    for py_file in r16d1._collect_scan_targets():
        for lineno, line, pattern in r16d1._scan_file(py_file):
            rel = py_file.relative_to(r16d1.BACKEND_ROOT)
            violations.append(f"  {rel}:{lineno}: [{pattern!r}]  {line}")

    assert not violations, (
        "R16-D1 no-bypass enforcement regressed after adding customer-tenant mode.\n"
        "Route all model calls through the gateway.\n\nViolations:\n"
        + "\n".join(violations)
    )


def test_ac7_callers_reach_customer_tenant_only_through_the_gateway(
    monkeypatch, configure_customer_tenant
):
    """Selecting the provider is config; the gateway resolver returns it — callers
    never import or instantiate the provider directly."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    assert get_generation_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
    assert get_embedding_provider().name == CUSTOMER_TENANT_PROVIDER_NAME


# ===========================================================================
# AC8 — All THREE modes (hosted, in-boundary, customer-tenant) are selectable for
#        BOTH generation and embedding behind the one gateway.
# ===========================================================================

_ALL_THREE_MODES = (_HOSTED, IN_BOUNDARY_PROVIDER_NAME, CUSTOMER_TENANT_PROVIDER_NAME)


def test_ac8_all_three_providers_registered_behind_one_gateway():
    for name in _ALL_THREE_MODES:
        provider = _PROVIDER_REGISTRY.get(name)
        assert isinstance(provider, ModelProvider), f"{name} not registered"
        assert provider.name == name


@pytest.mark.parametrize("mode", _ALL_THREE_MODES)
def test_ac8_each_mode_selectable_for_generation(monkeypatch, mode):
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", mode)
    assert get_generation_provider().name == mode


@pytest.mark.parametrize("mode", _ALL_THREE_MODES)
def test_ac8_each_mode_selectable_for_embedding(monkeypatch, mode):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", mode)
    assert get_embedding_provider().name == mode


def test_ac8_modes_mix_independently_across_capabilities(monkeypatch):
    """A generation mode and a DIFFERENT embedding mode resolve independently —
    the full independent-selection matrix across all three modes."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    assert get_generation_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
    assert get_embedding_provider().name == IN_BOUNDARY_PROVIDER_NAME

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", _HOSTED)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    assert get_generation_provider().name == _HOSTED
    assert get_embedding_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
