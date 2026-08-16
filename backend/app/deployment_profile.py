"""Deployment profile — WHO runs this deployment, and IS this process production.

This module answers **two orthogonal questions**. Do not collapse them.

``get_deployment_profile()`` — *who runs this deployment?*  (HP-2 T1)
    ``saas``
        CloudFulcrum runs it. Reaching an internet endpoint is ordinary, so the
        model gateway may keep a cloud-calling default.
    ``customer_hosted``
        The customer runs it, in their own boundary — on-prem, air-gapped, or
        under a data-residency constraint. A call that leaves the boundary must
        be a configured choice, never an inherited default (HP-2 AC1).

``is_production()`` — *is this process production?*  (R1.9.1-H1 T4)
    Keyed on ``ENVIRONMENT=production`` alone, and unchanged by this module.

**Why they are orthogonal.** A customer-hosted STAGING box is
``customer_hosted`` and NOT production: it must still refuse to inherit a cloud
default (its boundary is real), while it must NOT be subject to the production
credential rules that assume a managed secret store. A SaaS production box is
the mirror case. Deriving either from the other silently gets one of those two
deployments wrong, which is why they read different variables and neither
consults the other.

Reading the profile
-------------------
``DEPLOYMENT_PROFILE = 'saas' | 'customer_hosted'``

* **Unset or blank → ``saas``.** Backward compatible by design: every existing
  deployment predates this variable, and every one of them is SaaS. Nothing
  changes for them (HP-2 AC2).
* **Set to anything else → raises** :class:`InvalidDeploymentProfile`.

That last rule is where this module deliberately DIFFERS from
:mod:`app.network_profile`, which degrades an unrecognised value to its safe
default. The difference is not an inconsistency — the safe direction is opposite
in the two cases. An unknown ``NETWORK_PROFILE`` degrades toward the FULL
experience, so the worst outcome is a connect flow that is offered and fails. An
unknown ``DEPLOYMENT_PROFILE`` degrading to ``saas`` would hand a customer-hosted
deployment the cloud-calling default it exists to refuse — so a typo like
``customer-hosted`` or ``on_prem`` would silently reinstate the exact defect HP-2
removes. There is no safe default to degrade to, so we refuse instead.

Raising is also consistent with how this codebase treats a misconfigured closed
vocabulary elsewhere: ``telemetry.record_event`` and ``audit.log_event`` both
raise on an unregistered type, on the same reasoning — it is a deterministic
configuration error that request data cannot cause, so it is caught in review or
at boot, never in production traffic.

The vocabulary is CLOSED. ``on_prem`` / ``on-premise`` / ``customer-hosted`` are
deliberately NOT aliases for ``customer_hosted``: an operator who writes one gets
an immediate error naming the valid values, which is more useful than a silent
alias that grows a synonym list nobody can enumerate.

Consumers must resolve the profile through this module —
``tests/contract/test_deployment_profile.py`` fails the build on any other module
that reads ``DEPLOYMENT_PROFILE`` from the environment directly.
"""
from __future__ import annotations

import os
from typing import Literal

DeploymentProfile = Literal["saas", "customer_hosted"]

#: CloudFulcrum-operated. A cloud-calling default is acceptable here.
DEPLOYMENT_PROFILE_SAAS: DeploymentProfile = "saas"
#: Customer-operated (on-prem / air-gapped / data-residency constrained).
DEPLOYMENT_PROFILE_CUSTOMER_HOSTED: DeploymentProfile = "customer_hosted"

#: The closed set of recognised profiles, in documentation order.
VALID_DEPLOYMENT_PROFILES: tuple[DeploymentProfile, ...] = (
    DEPLOYMENT_PROFILE_SAAS,
    DEPLOYMENT_PROFILE_CUSTOMER_HOSTED,
)

#: Env var carrying the profile. Read ONLY here — see the module docstring.
ENV_DEPLOYMENT_PROFILE = "DEPLOYMENT_PROFILE"

#: Applied when the variable is unset or blank (backward compatible — HP-2 AC2).
DEFAULT_DEPLOYMENT_PROFILE: DeploymentProfile = DEPLOYMENT_PROFILE_SAAS


class InvalidDeploymentProfile(ValueError):
    """``DEPLOYMENT_PROFILE`` is set to a value outside the closed vocabulary.

    A ``ValueError`` subclass so a caller that already handles configuration
    errors generically keeps working, while a caller that wants to name this
    specific failure can catch it precisely.
    """


def get_deployment_profile() -> DeploymentProfile:
    """Return the deployment profile from the environment.

    Read live (not cached) so an operator or a test can change it without a code
    change — the value is trivial to resolve, and the same posture
    :func:`is_production` and :func:`app.network_profile.get_network_profile`
    already take.

    Returns:
        ``'saas'`` when the variable is unset or blank, otherwise the recognised
        profile it names. Matching is case-insensitive and tolerates surrounding
        whitespace, because ``DEPLOYMENT_PROFILE=Customer_Hosted`` in a hand-
        edited ``.env`` is an obvious intent, not an ambiguous one.

    Raises:
        InvalidDeploymentProfile: the variable is set to a non-blank value that
            is not a recognised profile. The message names the variable, the
            offending value and every valid value, so the fix needs no source
            reading.
    """
    raw = os.environ.get(ENV_DEPLOYMENT_PROFILE)
    if raw is None:
        return DEFAULT_DEPLOYMENT_PROFILE

    normalised = raw.strip().lower()
    if not normalised:
        # Blank is "not configured", which every pre-HP-2 deployment is.
        return DEFAULT_DEPLOYMENT_PROFILE
    if normalised in VALID_DEPLOYMENT_PROFILES:
        return normalised  # type: ignore[return-value]

    valid = ", ".join(f"'{p}'" for p in VALID_DEPLOYMENT_PROFILES)
    raise InvalidDeploymentProfile(
        f"{ENV_DEPLOYMENT_PROFILE}={raw!r} is not a recognised deployment "
        f"profile. Valid values: {valid}. Leave {ENV_DEPLOYMENT_PROFILE} unset "
        f"for '{DEFAULT_DEPLOYMENT_PROFILE}' (the default, unchanged for every "
        "existing deployment). This value is refused rather than defaulted "
        "because defaulting a customer-hosted deployment to "
        f"'{DEPLOYMENT_PROFILE_SAAS}' would give it the cloud-calling default it "
        "exists to refuse."
    )


def is_customer_hosted() -> bool:
    """True when the customer runs this deployment inside their own boundary.

    The gate HP-2.2 / HP-2.3 / HP-2.5 consult. Prefer this over comparing the
    result of :func:`get_deployment_profile` inline, so the comparison exists in
    one place.

    Raises:
        InvalidDeploymentProfile: propagated from :func:`get_deployment_profile`.
            Deliberately NOT swallowed into ``False`` — a misconfigured profile
            reported as "not customer-hosted" is precisely the silent reinstating
            of the cloud default this module refuses.
    """
    return get_deployment_profile() == DEPLOYMENT_PROFILE_CUSTOMER_HOSTED


def is_saas() -> bool:
    """True when CloudFulcrum runs this deployment (the default).

    Raises:
        InvalidDeploymentProfile: see :func:`is_customer_hosted`.
    """
    return get_deployment_profile() == DEPLOYMENT_PROFILE_SAAS


def validate_deployment_profile() -> DeploymentProfile:
    """Resolve the profile at startup so a bad value fails boot, not a request.

    :func:`get_deployment_profile` already raises on an unrecognised value, but
    only when something asks. Calling this from the application lifespan turns
    "the first request that happens to consult the profile 500s" into "the
    process refuses to start", which is where a configuration error belongs.

    Returns:
        The resolved profile, so the caller can log the posture it resolved.

    Raises:
        InvalidDeploymentProfile: the variable names an unrecognised profile.
    """
    return get_deployment_profile()


def is_production() -> bool:
    """True when this process should be treated as production.

    R1.9.1-H1 T4. Single source of truth for "is this process production": only
    an explicit ``ENVIRONMENT=production`` selects the production profile.
    Operational flags such as ``REQUIRE_CONNECTOR_SECRETS=1`` may enforce startup
    secret presence, but they do not redefine the deployment profile for staging,
    CI, or standalone runs.

    Orthogonal to :func:`get_deployment_profile` — see the module docstring.
    Unchanged by HP-2.

    Read live (not cached) — cheap to resolve and must reflect the current
    environment, e.g. in tests that toggle it via ``monkeypatch``.
    """
    return os.getenv("ENVIRONMENT", "").strip().lower() == "production"
