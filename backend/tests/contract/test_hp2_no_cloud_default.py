"""HP-2 T2 — no cloud-calling default under the customer-hosted profile.

``model_gateway/__init__.py`` defaulted BOTH ``MODEL_GENERATION_PROVIDER`` and
``MODEL_EMBEDDING_PROVIDER`` to ``hosted``, which reaches an external hosted API.
(The endpoint literal is deliberately not repeated here — the R16-D1 no-bypass
scanners sweep test files too, and the gateway package is the only place allowed
to name a provider endpoint.) An air-gapped or data-residency-constrained
on-prem deployment left at defaults
therefore produced findings with no AI narrative and no retrieval evidence while
reporting healthy — the silent degradation HP-2 exists to remove.

T2 makes the fallback conditional on the HP-2 T1 deployment profile.

Story ACs covered here:
  AC1 — under ``customer_hosted`` with the provider variables unset, startup
        FAILS with a message naming the variables and the valid values.
  AC2 — under ``saas``, unset variables still resolve to ``hosted``. Unchanged.

Plus the two rules that keep the change honest: the refusal fires from
``validate_provider_config()`` (the startup path) rather than from a per-call
resolution path, and SaaS resolution is byte-identical to before HP-2 — including
the pre-existing behaviour of a blank variable, which must NOT quietly become
``hosted`` as a side effect of this work.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import model_gateway
from app.model_gateway import (
    MissingProviderConfiguration,
    get_embedding_provider,
    get_generation_provider,
    resolve_provider_names,
    validate_provider_config,
)

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
_GATEWAY_SOURCE: Path = BACKEND_ROOT / "app" / "model_gateway" / "__init__.py"

_GEN = "MODEL_GENERATION_PROVIDER"
_EMB = "MODEL_EMBEDDING_PROVIDER"


@pytest.fixture
def clean_env(monkeypatch):
    """No provider vars, no profile — the state a fresh deployment boots in."""
    monkeypatch.delenv(_GEN, raising=False)
    monkeypatch.delenv(_EMB, raising=False)
    monkeypatch.delenv("DEPLOYMENT_PROFILE", raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# AC2 — the SaaS profile is unchanged
# ---------------------------------------------------------------------------


def test_saas_unset_still_resolves_to_hosted(clean_env):
    """AC2: the default profile behaves exactly as it did before HP-2."""
    assert get_generation_provider().name == "hosted"
    assert get_embedding_provider().name == "hosted"


def test_saas_explicit_profile_unset_vars_resolve_to_hosted(clean_env):
    clean_env.setenv("DEPLOYMENT_PROFILE", "saas")
    assert get_generation_provider().name == "hosted"
    assert get_embedding_provider().name == "hosted"


def test_saas_startup_validation_passes_with_nothing_configured(clean_env):
    """The out-of-the-box SaaS deployment still boots (T5-AC1 preserved)."""
    validate_provider_config()  # must not raise


def test_saas_independent_resolution_is_preserved(clean_env):
    """T2-AC3: the two roles still resolve independently."""
    clean_env.setenv(_EMB, "in_boundary")
    assert get_generation_provider().name == "hosted"
    assert get_embedding_provider().name == "in_boundary"


def test_saas_blank_value_behaviour_is_unchanged(clean_env):
    """A blank var must NOT silently become 'hosted' as a side effect of T2.

    Before HP-2 a blank value reached _resolve_provider and failed as an
    unregistered name. That is still the behaviour — widening blank to mean
    "unset" would be a real behaviour change smuggled in under "no change to
    SaaS".
    """
    clean_env.setenv(_GEN, "")
    with pytest.raises(ValueError) as exc:
        get_generation_provider()
    assert "not a registered model provider" in str(exc.value)
    assert not isinstance(exc.value, MissingProviderConfiguration)


def test_saas_unregistered_name_still_raises_the_original_error(clean_env):
    clean_env.setenv(_GEN, "nope")
    with pytest.raises(ValueError) as exc:
        validate_provider_config()
    assert "not a registered model provider" in str(exc.value)


# ---------------------------------------------------------------------------
# AC1 — customer_hosted has no cloud-calling default
# ---------------------------------------------------------------------------


def test_customer_hosted_both_unset_fails_startup(clean_env):
    """AC1: no cloud default is inherited; startup refuses."""
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    with pytest.raises(MissingProviderConfiguration):
        validate_provider_config()


def test_refusal_names_both_variables_and_every_valid_value(clean_env):
    """AC1: the operator must be able to fix it without reading source."""
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    with pytest.raises(MissingProviderConfiguration) as exc:
        validate_provider_config()
    message = str(exc.value)
    assert _GEN in message
    assert _EMB in message
    for valid in ("hosted", "in_boundary", "customer_tenant"):
        assert f"'{valid}'" in message, f"{valid} missing from: {message}"


def test_refusal_explains_why_there_is_no_default(clean_env):
    """A refusal an operator cannot rationalise gets worked around, not fixed."""
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    with pytest.raises(MissingProviderConfiguration) as exc:
        validate_provider_config()
    message = str(exc.value).lower()
    assert "customer_hosted" in message
    assert "boundary" in message


@pytest.mark.parametrize("configured,missing", [(_GEN, _EMB), (_EMB, _GEN)])
def test_customer_hosted_fails_when_only_one_is_set(clean_env, configured, missing):
    """Half-configured is still misconfigured — the unset one has no default.

    This is the likelier real mistake: an operator sets the generation provider,
    forgets embeddings, and the embedding path silently reaches the cloud (and
    returns [], disabling retrieval entirely).
    """
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    clean_env.setenv(configured, "in_boundary")
    with pytest.raises(MissingProviderConfiguration) as exc:
        validate_provider_config()
    assert missing in str(exc.value)


def test_customer_hosted_blank_is_also_refused(clean_env):
    """Blank is not a configured choice. No back-compat constraint in this profile."""
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    clean_env.setenv(_GEN, "   ")
    clean_env.setenv(_EMB, "in_boundary")
    with pytest.raises(MissingProviderConfiguration):
        validate_provider_config()


def test_customer_hosted_fully_configured_boots(clean_env):
    """The profile forbids an INHERITED cloud call, not a configured provider.

    The endpoint and the probe settings below are HP-2.3's doing, not HP-2.2's:
    once the posture probe exists, "configured" means an endpoint too, and naming
    a provider with no endpoint is refused under this profile. Reachability
    probing is switched off so this test stays about provider RESOLUTION —
    HP-2.3's own suite covers the posture rules.
    """
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    clean_env.setenv(_GEN, "in_boundary")
    clean_env.setenv(_EMB, "in_boundary")
    clean_env.setenv("IN_BOUNDARY_BASE_URL", "http://model.internal:11434")
    clean_env.setenv("MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS", "0")
    validate_provider_config()  # must not raise
    assert get_generation_provider().name == "in_boundary"
    assert get_embedding_provider().name == "in_boundary"


def test_customer_hosted_may_still_choose_hosted_deliberately(clean_env):
    """An explicit choice is allowed — the rule is about inheritance, not policy.

    HP-2 removes the *inherited* cloud call. A customer-hosted deployment that
    deliberately sets 'hosted' has made the decision, and HP-3 is what audits the
    resulting transmission.

    The credential and probe settings are HP-2.3's requirement: the hosted
    provider REQUIRES a credential, so under this profile choosing it without one
    is refused. Reachability probing is off so this stays a resolution test.
    """
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    clean_env.setenv(_GEN, "hosted")
    clean_env.setenv(_EMB, "hosted")
    clean_env.setenv("ANTHROPIC_API_KEY", "test-key-not-a-real-credential")
    clean_env.setenv("MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS", "0")
    validate_provider_config()  # must not raise
    assert get_generation_provider().name == "hosted"


def test_customer_hosted_getters_refuse_rather_than_reaching_the_cloud(clean_env):
    """A non-startup entry point (CLI, worker) must not silently call out.

    The getters do not carry their own copy of the rule — they share the one
    resolution helper — but they must not paper over it either.
    """
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    with pytest.raises(MissingProviderConfiguration):
        get_generation_provider()
    with pytest.raises(MissingProviderConfiguration):
        get_embedding_provider()


# ---------------------------------------------------------------------------
# The reproducibility record stays honest
# ---------------------------------------------------------------------------


def test_resolve_provider_names_reports_hosted_under_saas(clean_env):
    assert resolve_provider_names() == ("hosted", "hosted")


def test_resolve_provider_names_reports_none_when_nothing_is_configured(clean_env):
    """Naming 'hosted' here would record a provider the deployment never used."""
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    assert resolve_provider_names() == (None, None)


def test_resolve_provider_names_never_raises(clean_env):
    """Its contract is to describe the configuration, not refuse it."""
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    clean_env.setenv(_GEN, "not_a_real_provider")
    assert resolve_provider_names() == ("not_a_real_provider", None)


def test_reproducibility_record_tolerates_an_unconfigured_provider(clean_env):
    """The consumer already types these Optional — prove it end to end."""
    clean_env.setenv("DEPLOYMENT_PROFILE", "customer_hosted")
    from app.run_reproducibility import ai_mode_record

    record = ai_mode_record()
    assert record["generation_provider"] is None
    assert record["embedding_provider"] is None


# ---------------------------------------------------------------------------
# Structural — one fallback, and it lives on the startup path
# ---------------------------------------------------------------------------


def test_exactly_one_place_decides_the_fallback():
    """Before T2 the same os.getenv(var, _DEFAULT_PROVIDER) line sat in 4 places.

    That is the shape that lets one copy drift (HP-1's eight org guards). The
    fallback is now decided in ``_configured_provider_name`` alone.
    """
    tree = ast.parse(_GATEWAY_SOURCE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_env_read = (
            isinstance(func, ast.Attribute) and func.attr in {"getenv", "get"}
        )
        if not is_env_read or len(node.args) < 2:
            continue
        default = node.args[1]
        if isinstance(default, ast.Name) and default.id == "_DEFAULT_PROVIDER":
            offenders.append(ast.unparse(node))
    assert offenders == [], (
        "These read a MODEL_*_PROVIDER env var with _DEFAULT_PROVIDER as an "
        f"inline fallback instead of going through _configured_provider_name(): {offenders}"
    )


def test_default_is_profile_aware():
    """_default_provider_name must consult the profile, not return a constant."""
    code = inspect.getsource(model_gateway._default_provider_name)
    assert "is_customer_hosted" in code


def test_startup_validation_is_the_refusal_point():
    """The subtask's rule: refuse at startup, not per call."""
    code = inspect.getsource(model_gateway.validate_provider_config)
    assert "_configured_provider_name" in code


def test_missing_provider_configuration_is_a_value_error():
    """Existing generic handling of gateway config errors keeps working."""
    assert issubclass(MissingProviderConfiguration, ValueError)


def test_lifespan_still_calls_validate_provider_config():
    """The refusal only reaches boot if the lifespan keeps calling it."""
    from app import main as main_module

    source = inspect.getsource(main_module.lifespan)
    assert "validate_provider_config" in source
    # And the profile is resolved BEFORE the gateway is validated, so a bad
    # profile is reported as a bad profile rather than as a missing provider.
    assert source.index("validate_deployment_profile") < source.index(
        "validate_provider_config"
    )
