"""HP-2.3 — bounded startup posture probe for the configured model providers.

HP-2.2 stopped a customer-hosted deployment INHERITING a cloud provider. It could
not tell whether the provider that was configured is actually reachable: a
deployment pointed at an in-boundary model server nobody started still booted,
still reported healthy, and still produced findings with no AI narrative and no
retrieval evidence.

Covered here:
  * three checks per role — endpoint configuration, credential presence,
    reachability — with the FIRST failing one reported;
  * the checks are distinct (a missing credential never reads as "unreachable");
  * messages name the check, the variable and the endpoint host, and NEVER the
    credential value;
  * severity is profile-dependent: `customer_hosted` refuses to boot, `saas`
    records unhealthy posture and continues;
  * the probe is bounded — one attempt, no retries, an explicit timeout;
  * `unknown` never blocks boot, because "we did not look" is not "it is broken";
  * the posture is cached for HP-2.5 and reading it never re-probes.

Every test injects a fake connect function, so the suite makes no network call.
The single exception is explicitly marked and asserts only that a real connect to
an unroutable address returns WITHIN the timeout.
"""
from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from app import degradation
from app.model_gateway import probe
from app.model_gateway._interface import (
    ROLE_EMBEDDING,
    ROLE_GENERATION,
    GenerationRequest,
    GenerationResult,
    ModelProvider,
    ProviderProbeTarget,
)
from app.model_gateway.probe import (
    CHECK_CREDENTIAL,
    CHECK_ENDPOINT_CONFIG,
    CHECK_NOT_RUN,
    CHECK_REACHABILITY,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    ENV_DISABLE,
    ENV_PROBE_TIMEOUT,
    ProviderUnreachable,
    evaluate_startup_posture,
    enforce_startup_posture,
    probe_role,
    probe_timeout_seconds,
    probes_enabled,
    provider_posture,
    reset_posture,
    split_endpoint,
)

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
_PROBE_SOURCE: Path = BACKEND_ROOT / "app" / "model_gateway" / "probe.py"

_SECRET = "sk-ant-THIS-VALUE-MUST-NEVER-APPEAR"


class _FakeProvider(ModelProvider):
    """A provider whose probe target is dictated by the test."""

    def __init__(self, name: str, target: ProviderProbeTarget):
        self._name = name
        self._target = target

    @property
    def name(self) -> str:
        return self._name

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(ok=False, text=None)

    def embed(self, texts: List[str]) -> List[List[float]]:  # pragma: no cover
        return []

    def probe_target(self, role: str) -> ProviderProbeTarget:
        return self._target


def _reachable(*_args) -> Optional[str]:
    return None


def _unreachable(reason: str = "connection refused"):
    def _connect(*_args) -> Optional[str]:
        return reason

    return _connect


def _counting_connect(calls: List[Tuple[str, int, float]]):
    def _connect(host: str, port: int, timeout: float) -> Optional[str]:
        calls.append((host, port, timeout))
        return None

    return _connect


def _target(**kw) -> ProviderProbeTarget:
    base = dict(
        endpoint="http://model.internal:11434/v1/embeddings",
        endpoint_config_keys=("IN_BOUNDARY_BASE_URL",),
        credential_required=False,
        credential_present=True,
        credential_config_key="IN_BOUNDARY_API_KEY",
    )
    base.update(kw)
    return ProviderProbeTarget(**base)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Probing on, profile default, no cached posture."""
    monkeypatch.delenv(ENV_DISABLE, raising=False)
    monkeypatch.delenv(ENV_PROBE_TIMEOUT, raising=False)
    monkeypatch.delenv("DEPLOYMENT_PROFILE", raising=False)
    reset_posture()
    yield
    reset_posture()


# ---------------------------------------------------------------------------
# Endpoint parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.example.com/v1/messages", ("api.example.com", 443)),
        ("http://ollama.internal:11434", ("ollama.internal", 11434)),
        ("http://ollama.internal/v1/embeddings", ("ollama.internal", 80)),
        ("https://host.example.com:8443/x", ("host.example.com", 8443)),
    ],
)
def test_split_endpoint_resolves_host_and_port(url, expected):
    assert split_endpoint(url) == expected


@pytest.mark.parametrize("bad", ["", "   ", "not a url", "ollama.internal:11434"])
def test_split_endpoint_never_guesses(bad):
    """A URL with no scheme and no port yields None rather than an invented port.

    Probing a port we made up would report a failure the operator cannot act on.
    """
    assert split_endpoint(bad) is None


# ---------------------------------------------------------------------------
# The three checks, and their distinctness
# ---------------------------------------------------------------------------


def test_reachable_endpoint_is_ok():
    p = _FakeProvider("in_boundary", _target())
    result = probe_role(ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER", connect=_reachable)
    assert result.status == degradation.STATUS_OK
    assert result.check == CHECK_REACHABILITY
    assert result.probed is True
    assert result.healthy is True


def test_unreachable_endpoint_is_unavailable_and_names_the_host():
    p = _FakeProvider("in_boundary", _target())
    result = probe_role(
        ROLE_EMBEDDING, p, "MODEL_EMBEDDING_PROVIDER", connect=_unreachable()
    )
    assert result.status == degradation.STATUS_UNAVAILABLE
    assert result.check == CHECK_REACHABILITY
    assert result.endpoint_host == "model.internal"
    assert "model.internal" in result.detail
    assert "MODEL_EMBEDDING_PROVIDER" in result.detail
    assert "connection refused" in result.detail


def test_unconfigured_endpoint_names_the_variables_to_set():
    p = _FakeProvider(
        "in_boundary",
        _target(endpoint=None, endpoint_config_keys=("IN_BOUNDARY_BASE_URL", "IN_BOUNDARY_EMBEDDING_ENDPOINT")),
    )
    result = probe_role(ROLE_EMBEDDING, p, "MODEL_EMBEDDING_PROVIDER", connect=_reachable)
    assert result.status == degradation.STATUS_UNAVAILABLE
    assert result.check == CHECK_ENDPOINT_CONFIG
    assert "IN_BOUNDARY_BASE_URL" in result.detail
    assert "IN_BOUNDARY_EMBEDDING_ENDPOINT" in result.detail
    assert result.probed is False


def test_missing_required_credential_is_a_distinct_check_from_unreachable():
    """AC: a missing credential must not read as 'unreachable'.

    They have different causes and different fixes, so they get different check
    names — and the credential one is reported even when the host IS reachable,
    which is the case a reachability-only probe would call healthy.
    """
    p = _FakeProvider(
        "customer_tenant",
        _target(credential_required=True, credential_present=False,
                credential_config_key="CUSTOMER_TENANT_API_KEY"),
    )
    result = probe_role(
        ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER", connect=_reachable
    )
    assert result.check == CHECK_CREDENTIAL
    assert result.check != CHECK_REACHABILITY
    assert result.status == degradation.STATUS_UNAVAILABLE
    assert "CUSTOMER_TENANT_API_KEY" in result.detail


def test_credential_check_short_circuits_before_the_network():
    """No point opening a socket for a provider that is about to 401."""
    calls: List[Tuple[str, int, float]] = []
    p = _FakeProvider(
        "customer_tenant",
        _target(credential_required=True, credential_present=False),
    )
    probe_role(
        ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER",
        connect=_counting_connect(calls),
    )
    assert calls == []


def test_optional_credential_absent_is_not_a_fault():
    """Ollama and vLLM need no key — failing startup there would break the exact
    on-prem customer HP-2 exists to serve."""
    p = _FakeProvider(
        "in_boundary", _target(credential_required=False, credential_present=False)
    )
    result = probe_role(ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER", connect=_reachable)
    assert result.status == degradation.STATUS_OK
    assert result.check == CHECK_REACHABILITY


def test_unparseable_endpoint_is_unknown_not_ok():
    p = _FakeProvider("in_boundary", _target(endpoint="::::not-a-url"))
    result = probe_role(ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER", connect=_reachable)
    assert result.status == degradation.STATUS_UNKNOWN
    assert result.check == CHECK_ENDPOINT_CONFIG


def test_provider_declaring_nothing_to_probe_is_unknown():
    p = _FakeProvider("future_provider", ProviderProbeTarget())
    result = probe_role(ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER", connect=_reachable)
    assert result.status == degradation.STATUS_UNKNOWN
    assert result.check == CHECK_NOT_RUN


def test_a_raising_probe_target_does_not_break_startup():
    class _Exploding(_FakeProvider):
        def probe_target(self, role):
            raise RuntimeError("boom")

    p = _Exploding("bad", _target())
    result = probe_role(ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER", connect=_reachable)
    assert result.status == degradation.STATUS_UNKNOWN
    assert result.check == CHECK_NOT_RUN


# ---------------------------------------------------------------------------
# No credential value ever appears
# ---------------------------------------------------------------------------


def test_no_credential_value_reaches_the_report():
    """The report proves a credential is missing; it must never carry one."""
    p = _FakeProvider(
        "customer_tenant",
        _target(credential_required=True, credential_present=False,
                credential_config_key="CUSTOMER_TENANT_API_KEY"),
    )
    result = probe_role(ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER", connect=_reachable)
    rendered = repr(result.to_dict())
    assert _SECRET not in rendered
    # And the target type structurally cannot hold a value.
    assert not hasattr(ProviderProbeTarget(), "credential")
    fields = set(ProviderProbeTarget.__dataclass_fields__)
    assert "credential_value" not in fields
    assert fields == {
        "endpoint",
        "endpoint_config_keys",
        "credential_required",
        "credential_present",
        "credential_config_key",
    }


def test_probe_source_never_resolves_a_credential():
    """Structural: the probe must not be able to read a key even by accident."""
    code = _PROBE_SOURCE.read_text(encoding="utf-8")
    for forbidden in ("resolve_api_key", "ANTHROPIC_API_KEY", "api_key"):
        assert forbidden not in code, f"probe.py must not reference {forbidden}"


# ---------------------------------------------------------------------------
# Both roles, independently
# ---------------------------------------------------------------------------


def test_both_roles_are_probed_and_reported_independently():
    gen = _FakeProvider("hosted", _target(endpoint="https://gen.example.com/v1"))
    emb = _FakeProvider("in_boundary", _target(endpoint="http://emb.internal:11434/v1"))

    def _mixed(host: str, port: int, timeout: float) -> Optional[str]:
        return None if host == "gen.example.com" else "connection refused"

    posture = evaluate_startup_posture(
        gen, emb, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_mixed,
    )
    by_role = {r.role: r for r in posture.roles}
    assert by_role[ROLE_GENERATION].status == degradation.STATUS_OK
    assert by_role[ROLE_EMBEDDING].status == degradation.STATUS_UNAVAILABLE
    assert by_role[ROLE_GENERATION].provider == "hosted"
    assert by_role[ROLE_EMBEDDING].provider == "in_boundary"
    # Rolled up: as bad as the worst role, never as good as the best.
    assert posture.status == degradation.STATUS_UNAVAILABLE
    assert posture.healthy is False


def test_identical_endpoints_are_connected_once_but_reported_twice():
    """Both roles on one provider is the common case; it costs one connect.

    Reporting stays per role — the cache removes a redundant identical network
    call, not the independent verdict.
    """
    calls: List[Tuple[str, int, float]] = []
    same = _FakeProvider("hosted", _target(endpoint="https://one.example.com/v1"))
    posture = evaluate_startup_posture(
        same, same, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_counting_connect(calls),
    )
    assert len(calls) == 1
    assert len(posture.roles) == 2
    assert {r.role for r in posture.roles} == {ROLE_GENERATION, ROLE_EMBEDDING}


# ---------------------------------------------------------------------------
# Profile-dependent severity
# ---------------------------------------------------------------------------


def test_customer_hosted_unreachable_provider_fails_startup(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    p = _FakeProvider("in_boundary", _target())
    posture = evaluate_startup_posture(
        p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_unreachable(),
    )
    with pytest.raises(ProviderUnreachable) as exc:
        enforce_startup_posture(posture)
    message = str(exc.value)
    assert "customer_hosted" in message
    assert "model.internal" in message
    assert ENV_PROBE_TIMEOUT in message  # the documented escape hatch


def test_escape_hatch_is_offered_only_when_it_would_help(monkeypatch):
    """A remedy that does not work is worse than none.

    ``MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=0`` skips REACHABILITY probing only.
    The endpoint-configuration and credential checks are free and run regardless,
    so offering that setting for those failures sends the operator to a switch
    that changes nothing.
    """
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "customer_hosted")

    # Unreachable -> the hatch applies, so it is offered.
    reachable_fail = _FakeProvider("in_boundary", _target())
    posture = evaluate_startup_posture(
        reachable_fail, reachable_fail, "MODEL_GENERATION_PROVIDER",
        "MODEL_EMBEDDING_PROVIDER", connect=_unreachable(),
    )
    with pytest.raises(ProviderUnreachable) as exc:
        enforce_startup_posture(posture)
    assert ENV_PROBE_TIMEOUT in str(exc.value)

    # No endpoint configured -> the hatch would NOT help, so it is withheld.
    unconfigured = _FakeProvider("in_boundary", _target(endpoint=None))
    posture = evaluate_startup_posture(
        unconfigured, unconfigured, "MODEL_GENERATION_PROVIDER",
        "MODEL_EMBEDDING_PROVIDER", connect=_reachable,
    )
    with pytest.raises(ProviderUnreachable) as exc:
        enforce_startup_posture(posture)
    assert ENV_PROBE_TIMEOUT not in str(exc.value)

    # Missing required credential -> likewise withheld.
    no_cred = _FakeProvider(
        "customer_tenant",
        _target(credential_required=True, credential_present=False),
    )
    posture = evaluate_startup_posture(
        no_cred, no_cred, "MODEL_GENERATION_PROVIDER",
        "MODEL_EMBEDDING_PROVIDER", connect=_reachable,
    )
    with pytest.raises(ProviderUnreachable) as exc:
        enforce_startup_posture(posture)
    assert ENV_PROBE_TIMEOUT not in str(exc.value)


def test_saas_unreachable_provider_does_not_block_startup(monkeypatch):
    """A transient hosted-API blip must not stop the service coming up."""
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "saas")
    p = _FakeProvider("hosted", _target())
    posture = evaluate_startup_posture(
        p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_unreachable(),
    )
    enforce_startup_posture(posture)  # must not raise
    # But the posture is recorded unhealthy for HP-2.5 to report.
    assert posture.healthy is False
    assert posture.status == degradation.STATUS_UNAVAILABLE


def test_customer_hosted_missing_credential_also_fails_startup(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    p = _FakeProvider(
        "customer_tenant",
        _target(credential_required=True, credential_present=False,
                credential_config_key="CUSTOMER_TENANT_API_KEY"),
    )
    posture = evaluate_startup_posture(
        p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_reachable,
    )
    with pytest.raises(ProviderUnreachable) as exc:
        enforce_startup_posture(posture)
    assert "CUSTOMER_TENANT_API_KEY" in str(exc.value)


def test_customer_hosted_healthy_provider_boots(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    p = _FakeProvider("in_boundary", _target())
    posture = evaluate_startup_posture(
        p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_reachable,
    )
    enforce_startup_posture(posture)  # must not raise
    assert posture.healthy is True


def test_unknown_never_blocks_boot_even_under_customer_hosted(monkeypatch):
    """'We did not look' must not be reported as 'it is broken'."""
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    p = _FakeProvider("future_provider", ProviderProbeTarget())
    posture = evaluate_startup_posture(
        p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_reachable,
    )
    assert posture.status == degradation.STATUS_UNKNOWN
    enforce_startup_posture(posture)  # must not raise


def test_disabled_probe_never_blocks_boot_under_customer_hosted(monkeypatch):
    """The documented escape hatch must actually work."""
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    monkeypatch.setenv(ENV_PROBE_TIMEOUT, "0")
    calls: List[Tuple[str, int, float]] = []
    p = _FakeProvider("in_boundary", _target())
    posture = evaluate_startup_posture(
        p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_counting_connect(calls),
    )
    assert calls == []
    assert posture.status == degradation.STATUS_UNKNOWN
    enforce_startup_posture(posture)  # must not raise


# ---------------------------------------------------------------------------
# Bounded, and suppressible
# ---------------------------------------------------------------------------


def test_default_timeout_is_documented_and_positive():
    assert probe_timeout_seconds() == DEFAULT_PROBE_TIMEOUT_SECONDS
    assert 0 < DEFAULT_PROBE_TIMEOUT_SECONDS <= 10


def test_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv(ENV_PROBE_TIMEOUT, "1.5")
    assert probe_timeout_seconds() == 1.5
    calls: List[Tuple[str, int, float]] = []
    p = _FakeProvider("in_boundary", _target())
    probe_role(ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER",
               connect=_counting_connect(calls))
    assert calls[0][2] == 1.5


def test_malformed_timeout_falls_back_rather_than_failing_boot(monkeypatch):
    monkeypatch.setenv(ENV_PROBE_TIMEOUT, "not-a-number")
    assert probe_timeout_seconds() == DEFAULT_PROBE_TIMEOUT_SECONDS


@pytest.mark.parametrize("value,enabled", [("0", False), ("0.0", False), ("2", True)])
def test_zero_timeout_disables_probing(monkeypatch, value, enabled):
    monkeypatch.setenv(ENV_PROBE_TIMEOUT, value)
    assert probes_enabled() is enabled


def test_test_isolation_flag_suppresses_probing(monkeypatch):
    """The contract conftest sets this, so no test reaches the network by default."""
    monkeypatch.setenv(ENV_DISABLE, "1")
    assert probes_enabled() is False


def test_probe_makes_no_retries():
    """Bounded means one attempt. A retrying probe would reproduce the very
    startup hang this story exists to make loud."""
    calls: List[Tuple[str, int, float]] = []

    def _always_fails(host, port, timeout):
        calls.append((host, port, timeout))
        return "connection refused"

    p = _FakeProvider("in_boundary", _target())
    probe_role(ROLE_GENERATION, p, "MODEL_GENERATION_PROVIDER", connect=_always_fails)
    assert len(calls) == 1


@pytest.mark.parametrize("_", [0])
def test_real_connect_to_an_unroutable_address_returns_within_the_timeout(_):
    """The ONE test that touches the network stack.

    198.51.100.0/24 is RFC 5737 TEST-NET-2 — reserved and unroutable, so this
    cannot reach a real service. Asserts only that a real connect gives up within
    the bound: whether it times out or fails immediately with
    network-unreachable, both are a non-None reason and both are bounded.
    """
    timeout = 1.0
    started = time.monotonic()
    reason = probe.tcp_connect("198.51.100.1", 443, timeout)
    elapsed = time.monotonic() - started
    assert reason is not None
    assert elapsed < timeout + 3.0, f"probe took {elapsed:.1f}s, bound was {timeout}s"


# ---------------------------------------------------------------------------
# The posture is cached for HP-2.5
# ---------------------------------------------------------------------------


def test_posture_is_recorded_and_readable_without_reprobing():
    """A health check that opened sockets per request would be a DoS lever
    pointed at the customer's own model server."""
    calls: List[Tuple[str, int, float]] = []
    p = _FakeProvider("in_boundary", _target())
    evaluate_startup_posture(
        p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_counting_connect(calls),
    )
    before = len(calls)
    for _ in range(5):
        assert provider_posture() is not None
    assert len(calls) == before, "reading the posture must not re-probe"


def test_posture_is_none_before_evaluation():
    reset_posture()
    assert provider_posture() is None


def test_posture_serialises_for_a_health_payload():
    p = _FakeProvider("in_boundary", _target())
    posture = evaluate_startup_posture(
        p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
        connect=_reachable,
    )
    payload = posture.to_dict()
    assert payload["status"] == degradation.STATUS_OK
    assert set(payload["roles"]) == {ROLE_GENERATION, ROLE_EMBEDDING}
    role = payload["roles"][ROLE_GENERATION]
    for key in ("role", "provider", "variable", "status", "check", "detail",
                "endpointHost", "probed"):
        assert key in role


def test_gateway_reexports_the_posture():
    """HP-2.5 should read the gateway's public interface, not a submodule."""
    from app import model_gateway

    assert hasattr(model_gateway, "provider_posture")
    assert "provider_posture" in model_gateway.__all__


# ---------------------------------------------------------------------------
# Structural — one vocabulary, one startup entry point
# ---------------------------------------------------------------------------


def test_every_status_comes_from_the_canonical_set():
    """No sixth vocabulary — degradation.py is the one status set."""
    p = _FakeProvider("in_boundary", _target())
    for connect in (_reachable, _unreachable()):
        posture = evaluate_startup_posture(
            p, p, "MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER",
            connect=connect,
        )
        for role in posture.roles:
            assert role.status in degradation.CANONICAL_STATUSES


def test_probe_defines_no_status_literals():
    """Statuses must be imported from degradation, never restated here."""
    tree = ast.parse(_PROBE_SOURCE.read_text(encoding="utf-8"))
    assigned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.startswith("STATUS_"):
                    assigned.add(t.id)
    assert assigned == set(), f"probe.py restates canonical statuses: {assigned}"


def test_probe_is_wired_into_the_single_startup_validator():
    """Extends validate_provider_config rather than adding a parallel validator."""
    from app import model_gateway

    code = inspect.getsource(model_gateway.validate_provider_config)
    assert "evaluate_startup_posture" in code
    assert "enforce_startup_posture" in code


def test_evaluation_is_guarded_but_enforcement_is_not():
    """A probing BUG must not block startup; a deliberate REFUSAL must not be
    swallowed by the same guard."""
    from app import model_gateway

    code = inspect.getsource(model_gateway.validate_provider_config)
    eval_at = code.index("evaluate_startup_posture")
    enforce_at = code.index("enforce_startup_posture")
    except_at = code.index("except Exception", eval_at)
    assert eval_at < except_at < enforce_at, (
        "enforce_startup_posture must sit OUTSIDE the try/except guarding evaluation"
    )
