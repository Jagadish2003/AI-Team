"""HP-2.5 — model-provider posture on GET /api/health (story AC3, part 2).

``/api/health`` reported ``healthy`` on database connectivity alone, so a
deployment whose configured model provider was unreachable looked completely fine
while producing findings with no AI narrative and no retrieval evidence. HP-2.3
resolves the posture at startup; this makes the surface an operator actually
watches tell the truth about it.

Covered here:
  * an unreachable provider drives ``status: unhealthy`` (AC3);
  * the ``checks`` block names each role's provider and status;
  * statuses come from the canonical set, and an unestablished posture is
    ``unknown`` — never ``ok``;
  * ``unknown`` does NOT make the service unhealthy: reporting a deployment as
    broken because nobody looked is the mirror image of the defect being fixed,
    and it is the same rule HP-2.3 uses when deciding whether to refuse boot;
  * the legacy ``{ok, ts}`` fields and the ``database`` check are unchanged;
  * **the endpoint is PUBLIC**, so the payload carries no endpoint host and the
    read never re-probes.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from app import degradation
from app.model_gateway._interface import ROLE_EMBEDDING, ROLE_GENERATION
from app.model_gateway.probe import (
    CHECK_CREDENTIAL,
    CHECK_ENDPOINT_CONFIG,
    CHECK_NOT_RUN,
    CHECK_REACHABILITY,
    ProviderPosture,
    ProviderProbe,
)
from app.model_provider_health import (
    degrades_overall_health,
    model_provider_health,
    role_degrades_health,
)

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
_HEALTH_SOURCE: Path = BACKEND_ROOT / "app" / "model_provider_health.py"

_SECRET_HOST = "ollama.internal.bank.example"


def _probe(role: str, status: str, provider: str = "in_boundary",
           check: str = "reachability", host: Optional[str] = _SECRET_HOST,
           probed: bool = True) -> ProviderProbe:
    return ProviderProbe(
        role=role,
        provider=provider,
        env_var=f"MODEL_{role.upper()}_PROVIDER",
        status=status,
        check=check,
        detail=f"{role}: {host}:11434 is not reachable (connection refused).",
        endpoint_host=host,
        probed=probed,
    )


def _posture(gen_status: str, emb_status: str, **kw) -> ProviderPosture:
    return ProviderPosture(
        roles=[
            _probe(ROLE_GENERATION, gen_status, **kw),
            _probe(ROLE_EMBEDDING, emb_status, **kw),
        ]
    )


def _with_posture(posture):
    return patch("app.model_gateway.provider_posture", return_value=posture)


# ---------------------------------------------------------------------------
# The shaping helper
# ---------------------------------------------------------------------------


def test_healthy_posture_is_reported_ok():
    with _with_posture(_posture(degradation.STATUS_OK, degradation.STATUS_OK)):
        result = model_provider_health()
    assert result["status"] == degradation.STATUS_OK
    assert set(result["roles"]) == {ROLE_GENERATION, ROLE_EMBEDDING}


def test_each_role_names_its_provider_and_status():
    with _with_posture(
        _posture(degradation.STATUS_OK, degradation.STATUS_UNAVAILABLE)
    ):
        result = model_provider_health()
    assert result["roles"][ROLE_GENERATION]["provider"] == "in_boundary"
    assert result["roles"][ROLE_GENERATION]["status"] == degradation.STATUS_OK
    assert result["roles"][ROLE_EMBEDDING]["status"] == degradation.STATUS_UNAVAILABLE
    # Rolled up as bad as the worst role.
    assert result["status"] == degradation.STATUS_UNAVAILABLE


def test_no_posture_is_unknown_not_ok():
    with _with_posture(None):
        result = model_provider_health()
    assert result["status"] == degradation.STATUS_UNKNOWN
    assert result["reason"] == "not_evaluated"


def test_an_exploding_posture_read_is_unknown_not_ok():
    """A health check must not 500 the endpoint that reports health."""
    with patch("app.model_gateway.provider_posture", side_effect=RuntimeError("boom")):
        result = model_provider_health()
    assert result["status"] == degradation.STATUS_UNKNOWN
    assert result["reason"] == "posture_unavailable"


def test_statuses_come_from_the_canonical_set():
    for status in degradation.CANONICAL_STATUSES:
        with _with_posture(_posture(status, status)):
            result = model_provider_health()
        assert result["status"] in degradation.CANONICAL_STATUSES


@pytest.mark.parametrize(
    "status,degrades",
    [
        (degradation.STATUS_OK, False),
        (degradation.STATUS_UNKNOWN, False),   # "we did not look" is not "broken"
        (degradation.STATUS_PARTIAL, True),
        (degradation.STATUS_UNAVAILABLE, True),
        (degradation.STATUS_FAILED, True),
    ],
)
def test_which_statuses_degrade_a_reachability_check(status, degrades):
    assert role_degrades_health(status, CHECK_REACHABILITY) is degrades


@pytest.mark.parametrize("check", [CHECK_CREDENTIAL, CHECK_ENDPOINT_CONFIG, CHECK_NOT_RUN])
def test_only_a_reachability_failure_degrades_the_verdict(check):
    """A missing credential or unconfigured endpoint is REPORTED but not degrading.

    Under customer_hosted HP-2.3 already refuses boot for both, so a running
    deployment in the profile where the model is load-bearing cannot be in this
    state. Under saas they are supported: LLM enrichment is optional by design —
    the deterministic fallbacks work with no ANTHROPIC_API_KEY, and the shipped
    dev/test setup has none. Flipping those to unhealthy makes the signal noise.
    """
    assert role_degrades_health(degradation.STATUS_UNAVAILABLE, check) is False


def test_a_missing_credential_is_still_reported_honestly():
    """Not degrading is not the same as hidden."""
    with _with_posture(
        _posture(
            degradation.STATUS_UNAVAILABLE,
            degradation.STATUS_UNAVAILABLE,
            check=CHECK_CREDENTIAL,
        )
    ):
        result = model_provider_health()
    assert result["status"] == degradation.STATUS_UNAVAILABLE
    assert degrades_overall_health(result) is False
    for role in result["roles"].values():
        assert role["check"] == CHECK_CREDENTIAL
        assert role["status"] == degradation.STATUS_UNAVAILABLE


# ---------------------------------------------------------------------------
# The endpoint is PUBLIC — no topology may leak
# ---------------------------------------------------------------------------


def test_no_endpoint_host_reaches_the_payload():
    """`/api/health` needs no credential, so an internal model host must not
    appear in it. The full detail stays in the startup log."""
    with _with_posture(
        _posture(degradation.STATUS_UNAVAILABLE, degradation.STATUS_UNAVAILABLE)
    ):
        result = model_provider_health()
    assert _SECRET_HOST not in repr(result)
    for role in result["roles"].values():
        assert "endpointHost" not in role
        assert "detail" not in role
        assert set(role) == {"provider", "status", "check", "probed"}


def test_endpoint_response_carries_no_host(client):
    with _with_posture(
        _posture(degradation.STATUS_UNAVAILABLE, degradation.STATUS_UNAVAILABLE)
    ):
        resp = client.get("/api/health")
    assert _SECRET_HOST not in resp.text


def test_health_read_never_reprobes():
    """A public endpoint that opened sockets per request would be a lever against
    the customer's own model server."""
    calls = []

    def _counting():
        calls.append(1)
        return _posture(degradation.STATUS_OK, degradation.STATUS_OK)

    with patch("app.model_gateway.provider_posture", side_effect=_counting):
        for _ in range(5):
            model_provider_health()
    assert len(calls) == 5  # read the CACHE each time...
    code = _HEALTH_SOURCE.read_text(encoding="utf-8")
    for forbidden in ("evaluate_startup_posture", "tcp_connect", "probe_role"):
        assert forbidden not in code, f"health must not call {forbidden}"


# ---------------------------------------------------------------------------
# The endpoint itself (AC3)
# ---------------------------------------------------------------------------


def test_unreachable_provider_makes_the_service_unhealthy(client):
    """Story AC3: it never reports healthy with a configured provider down."""
    with _with_posture(
        _posture(degradation.STATUS_OK, degradation.STATUS_UNAVAILABLE)
    ):
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["ok"] is False
    assert body["checks"]["model_providers"]["status"] == degradation.STATUS_UNAVAILABLE


def test_healthy_providers_still_report_healthy(client):
    with _with_posture(_posture(degradation.STATUS_OK, degradation.STATUS_OK)):
        resp = client.get("/api/health")
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["ok"] is True


def test_unknown_posture_does_not_make_the_service_unhealthy(client):
    """The state every dev box and every contract run is in (probing disabled).

    Reporting them unhealthy for not having looked would be the mirror image of
    the defect HP-2 fixes, and it is the same rule HP-2.3 applies at boot.
    """
    with _with_posture(None):
        resp = client.get("/api/health")
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["ok"] is True
    # ...but the check itself is honest about not knowing.
    assert body["checks"]["model_providers"]["status"] == degradation.STATUS_UNKNOWN


def test_endpoint_names_both_roles(client):
    with _with_posture(_posture(degradation.STATUS_OK, degradation.STATUS_OK)):
        resp = client.get("/api/health")
    roles = resp.json()["checks"]["model_providers"]["roles"]
    assert set(roles) == {ROLE_GENERATION, ROLE_EMBEDDING}
    assert roles[ROLE_EMBEDDING]["provider"] == "in_boundary"


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_legacy_fields_are_retained(client):
    """Existing consumers read {ok, ts}; the database check predates HP-2."""
    resp = client.get("/api/health")
    body = resp.json()
    assert "ok" in body and isinstance(body["ok"], bool)
    assert "ts" in body
    assert "status" in body
    assert body["checks"]["database"]["status"] in ("ok", "error")


def test_default_test_environment_still_reports_ok(client):
    """Pins the existing contract test's expectation (test_contract_endpoints).

    Contract runs disable probing, so the posture is unknown — and the endpoint
    must still report ok, or every existing consumer breaks.
    """
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_plain_health_endpoint_is_untouched(client):
    """`/health` is the dumb liveness probe; HP-2.5 changes only `/api/health`."""
    resp = client.get("/health")
    body = resp.json()
    assert body["ok"] is True
    assert "checks" not in body


def test_endpoint_stays_public(client):
    """No credential is required — which is exactly why no host is reported."""
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_health_uses_the_shared_degradation_vocabulary():
    """No sixth status vocabulary on the health surface either."""
    code = _HEALTH_SOURCE.read_text(encoding="utf-8")
    assert "from app import degradation" in code
    assert 'STATUS_OK = "' not in code  # never restated locally


def test_main_composes_the_check_from_the_helper():
    from app import main as main_module

    code = inspect.getsource(main_module.api_health)
    assert "model_provider_health" in code
    assert "degrades_overall_health" in code
