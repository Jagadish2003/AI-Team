"""HP-2.3 — bounded startup posture probe for the configured model providers.

HP-2.2 stopped a customer-hosted deployment from INHERITING a cloud provider. It
cannot tell whether the provider that *was* configured is actually reachable. A
deployment pointed at ``http://ollama.internal:11434`` that nobody ever started
still boots, still reports healthy, and still produces findings with no AI
narrative and no retrieval evidence — the same silent degradation HP-2 exists to
remove, one step further along.

This module resolves and validates three things per role, cheaply, at startup:

1. **Endpoint configuration** — is there an endpoint to talk to at all?
2. **Credential presence** — for a provider that requires one. Presence only;
   the value is never read, logged, or carried.
3. **Reachability** — can a TCP connection be opened to the endpoint's host/port?

Failures are **loud and specific**: which check, which variable, which endpoint
host.

Why a TCP connect rather than a real model call
-----------------------------------------------
A model call costs money, needs a valid credential, and can fail for a dozen
reasons that have nothing to do with reachability — so it could not tell
"unreachable" from "wrong key", which the acceptance criteria require to be
distinct. A connect to ``(host, port)`` answers exactly the question asked, needs
no credential, costs nothing, is trivially bounded, and makes no assumption about
which paths the endpoint serves (an OpenAI-compatible server may legitimately 404
on ``GET /``).

Bounded means bounded
---------------------
An explicit short timeout and **no retries**. Startup hanging on an unreachable
host is the precise failure this story exists to make loud, so a probe that
retried would reproduce it. Results are cached per ``(host, port)`` within one
posture evaluation, so the common case where both roles resolve to the same
provider and endpoint costs one connect rather than two — each role is still
evaluated and REPORTED independently.

Severity is profile-dependent
-----------------------------
* ``customer_hosted`` — a configured provider that is unreachable, unconfigured,
  or missing a required credential **fails startup**. There is no fallback in
  that profile, so the deployment cannot do its job and should say so at boot.
* ``saas`` — the same conditions are recorded as unhealthy posture and logged,
  but do **not** block startup. A transient blip on a hosted API must not stop
  the service from coming up; HP-2.5 is what makes ``/api/health`` report it.

Statuses come from :mod:`app.degradation`, the platform's one canonical set —
this module introduces no sixth vocabulary. A probe that did not run is
``unknown``, never ``ok``.
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from app import degradation
from app.deployment_profile import is_customer_hosted
from app.model_gateway._interface import (
    ROLE_EMBEDDING,
    ROLE_GENERATION,
    ModelProvider,
    ProviderProbeTarget,
)

logger = logging.getLogger(__name__)

#: Wall-clock bound for a single connection attempt. ``0`` disables probing.
ENV_PROBE_TIMEOUT = "MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS"
DEFAULT_PROBE_TIMEOUT_SECONDS: float = 3.0

#: The existing switch tests and isolated runs already set. Reused rather than
#: inventing a second flag: a startup network call is exactly the class of work
#: it exists to suppress, and the contract conftest already sets it, so no test
#: reaches the network by default.
ENV_DISABLE = "AGENTIQ_DISABLE_BACKGROUND_JOBS"

#: Which check produced a role's status. Reported so a message can name it.
CHECK_ENDPOINT_CONFIG = "endpoint_configuration"
CHECK_CREDENTIAL = "credential_presence"
CHECK_REACHABILITY = "reachability"
CHECK_NOT_RUN = "not_run"

#: Native words this module hands to ``degradation.canonical_status``.
_NATIVE_OK = "ok"
_NATIVE_UNREACHABLE = "unreachable"
_NATIVE_CREDENTIAL_MISSING = "credential_missing"
_NATIVE_NOT_CONFIGURED = "not_configured"
_NATIVE_NOT_PROBED = "not_probed"

_DEFAULT_PORTS: Dict[str, int] = {"http": 80, "https": 443}

#: A connect function: (host, port, timeout) -> None on success, else a reason.
ConnectFn = Callable[[str, int, float], Optional[str]]


class ProviderUnreachable(RuntimeError):
    """A configured provider failed its startup posture check.

    Raised only under the ``customer_hosted`` profile, where there is no fallback
    and a deployment that cannot reach its model should not pretend to be
    working. A ``RuntimeError`` rather than a ``ValueError`` because the
    configuration is well-formed — the environment is not cooperating. (Contrast
    ``MissingProviderConfiguration``, which IS a value problem.)
    """


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def probe_timeout_seconds() -> float:
    """The per-attempt timeout, from the environment.

    ``0`` (or negative) disables probing entirely. An unparseable value falls
    back to the default with a warning rather than raising — a malformed timeout
    must not be the reason a deployment cannot boot.
    """
    raw = (os.environ.get(ENV_PROBE_TIMEOUT) or "").strip()
    if not raw:
        return DEFAULT_PROBE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number — using the default %.1fs.",
            ENV_PROBE_TIMEOUT,
            raw,
            DEFAULT_PROBE_TIMEOUT_SECONDS,
        )
        return DEFAULT_PROBE_TIMEOUT_SECONDS
    return max(value, 0.0)


def probes_enabled() -> bool:
    """Whether the reachability probe should run at all."""
    if os.environ.get(ENV_DISABLE) == "1":
        return False
    return probe_timeout_seconds() > 0.0


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderProbe:
    """One role's startup posture. Contains no secret value."""

    role: str
    provider: str
    env_var: str
    status: str
    check: str
    detail: str
    endpoint_host: Optional[str] = None
    probed: bool = False

    @property
    def healthy(self) -> bool:
        return degradation.is_healthy(self.status)

    def to_dict(self) -> Dict[str, object]:
        return {
            "role": self.role,
            "provider": self.provider,
            "variable": self.env_var,
            "status": self.status,
            "check": self.check,
            "detail": self.detail,
            "endpointHost": self.endpoint_host,
            "probed": self.probed,
        }


@dataclass(frozen=True)
class ProviderPosture:
    """Both roles, plus the rolled-up verdict HP-2.5 renders."""

    roles: List[ProviderProbe] = field(default_factory=list)

    @property
    def status(self) -> str:
        """As bad as the worst role. An empty posture is UNKNOWN, never ok."""
        return degradation.worst([r.status for r in self.roles])

    @property
    def healthy(self) -> bool:
        return degradation.is_healthy(self.status)

    def unhealthy_roles(self) -> List[ProviderProbe]:
        return [r for r in self.roles if not r.healthy]

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "roles": {r.role: r.to_dict() for r in self.roles},
        }


#: Last evaluated posture, for HP-2.5's health endpoint to read WITHOUT
#: re-probing — a health check that opened sockets on every request would be a
#: denial-of-service lever pointed at the customer's own model server.
_LAST_POSTURE: Optional[ProviderPosture] = None


def provider_posture() -> Optional[ProviderPosture]:
    """The posture resolved at startup, or ``None`` if it was never evaluated."""
    return _LAST_POSTURE


def _record(posture: ProviderPosture) -> None:
    global _LAST_POSTURE
    _LAST_POSTURE = posture


def reset_posture() -> None:
    """Clear the cached posture. For tests; never called by production code."""
    global _LAST_POSTURE
    _LAST_POSTURE = None


# ---------------------------------------------------------------------------
# The probe itself
# ---------------------------------------------------------------------------


def split_endpoint(url: str) -> Optional[Tuple[str, int]]:
    """``(host, port)`` for a URL, or ``None`` when no host can be determined.

    The port comes from the URL when explicit, else from the scheme. A URL with
    no recognisable scheme AND no explicit port yields ``None`` rather than a
    guess — probing a port we invented would report a failure the operator
    cannot act on.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    port = parts.port
    if port is None:
        port = _DEFAULT_PORTS.get((parts.scheme or "").lower())
    if port is None:
        return None
    return (host, int(port))


def tcp_connect(host: str, port: int, timeout: float) -> Optional[str]:
    """Open and immediately close a TCP connection. ``None`` on success.

    One attempt, no retries. The returned reason is a short non-secret phrase
    safe to log and to render in a health payload.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.timeout:
        return f"no response within {timeout:g}s"
    except socket.gaierror:
        return "host name could not be resolved"
    except ConnectionRefusedError:
        return "connection refused"
    except OSError as exc:
        # Covers network-unreachable, no-route, permission-denied. Report the
        # errno text, which is generic and carries nothing sensitive.
        return (exc.strerror or "connection failed").lower()


def probe_role(
    role: str,
    provider: ModelProvider,
    env_var: str,
    *,
    timeout: Optional[float] = None,
    enabled: Optional[bool] = None,
    connect: ConnectFn = tcp_connect,
    connect_cache: Optional[Dict[Tuple[str, int], Optional[str]]] = None,
) -> ProviderProbe:
    """Evaluate one role's posture. Never raises.

    Checks run cheapest-and-most-fundamental first, and the FIRST failing check
    is what gets reported: an endpoint that is not configured makes reachability
    meaningless, and a missing required credential would let a successful connect
    report ``ok`` for a provider whose every call is about to 401.
    """
    resolved_timeout = probe_timeout_seconds() if timeout is None else timeout
    resolved_enabled = probes_enabled() if enabled is None else enabled

    try:
        target = provider.probe_target(role)
    except Exception:  # noqa: BLE001 — a provider hook must not break startup
        logger.debug(
            "model_gateway.probe: %s provider %s probe_target() raised",
            role,
            provider.name,
            exc_info=True,
        )
        return ProviderProbe(
            role=role,
            provider=provider.name,
            env_var=env_var,
            status=degradation.STATUS_UNKNOWN,
            check=CHECK_NOT_RUN,
            detail=(
                f"{role}: provider '{provider.name}' could not describe what to "
                "probe, so its posture is unknown."
            ),
        )

    if not isinstance(target, ProviderProbeTarget):  # a provider returning junk
        target = ProviderProbeTarget()

    # 1. Nothing to probe at all — a provider with no network endpoint concept.
    if not target.endpoint and not target.endpoint_config_keys:
        return ProviderProbe(
            role=role,
            provider=provider.name,
            env_var=env_var,
            status=degradation.STATUS_UNKNOWN,
            check=CHECK_NOT_RUN,
            detail=(
                f"{role}: provider '{provider.name}' declares no endpoint to "
                "probe, so its posture is unknown."
            ),
        )

    # 2. Endpoint expected but not configured.
    if not target.endpoint:
        keys = " / ".join(target.endpoint_config_keys)
        return ProviderProbe(
            role=role,
            provider=provider.name,
            env_var=env_var,
            status=degradation.canonical_status(_NATIVE_NOT_CONFIGURED),
            check=CHECK_ENDPOINT_CONFIG,
            # Deliberately worded differently from the provider's own validate()
            # warning, which advises about the same misconfiguration generically.
            # This one adds the part validate() cannot: WHICH ROLE is affected,
            # and it is the reason a customer_hosted boot is refused.
            detail=(
                f"{role}: {env_var}={provider.name} has no endpoint configured "
                f"for this role — set {keys}. Every {role} call will fail."
            ),
        )

    parsed = split_endpoint(target.endpoint)
    host = parsed[0] if parsed else None

    # 3. Required credential missing. Checked BEFORE reachability: the connect
    #    would succeed and report ok for a provider about to fail on auth.
    if target.credential_required and not target.credential_present:
        key = target.credential_config_key or "the provider credential"
        return ProviderProbe(
            role=role,
            provider=provider.name,
            env_var=env_var,
            status=degradation.canonical_status(_NATIVE_CREDENTIAL_MISSING),
            check=CHECK_CREDENTIAL,
            endpoint_host=host,
            detail=(
                f"{role}: {env_var}={provider.name} requires a credential and "
                f"none is configured — set {key}. The endpoint "
                f"({host or 'unknown host'}) may be reachable, but every call "
                "will be rejected."
            ),
        )

    # 4. Unparseable endpoint — cannot probe, and will not guess a port.
    if parsed is None:
        keys = " / ".join(target.endpoint_config_keys) or env_var
        return ProviderProbe(
            role=role,
            provider=provider.name,
            env_var=env_var,
            status=degradation.STATUS_UNKNOWN,
            check=CHECK_ENDPOINT_CONFIG,
            detail=(
                f"{role}: the configured endpoint has no resolvable host and "
                f"port, so reachability is unknown — check {keys}."
            ),
        )

    # 5. Probing disabled — explicitly unknown, never ok.
    if not resolved_enabled:
        return ProviderProbe(
            role=role,
            provider=provider.name,
            env_var=env_var,
            status=degradation.canonical_status(_NATIVE_NOT_PROBED),
            check=CHECK_NOT_RUN,
            endpoint_host=host,
            detail=(
                f"{role}: reachability of {host} was not probed "
                f"({ENV_PROBE_TIMEOUT}=0 or {ENV_DISABLE}=1), so its posture is "
                "unknown."
            ),
        )

    # 6. Reachability.
    port = parsed[1]
    cache_key = (host or "", port)
    if connect_cache is not None and cache_key in connect_cache:
        reason = connect_cache[cache_key]
    else:
        reason = connect(host or "", port, resolved_timeout)
        if connect_cache is not None:
            connect_cache[cache_key] = reason

    if reason is None:
        return ProviderProbe(
            role=role,
            provider=provider.name,
            env_var=env_var,
            status=degradation.canonical_status(_NATIVE_OK),
            check=CHECK_REACHABILITY,
            endpoint_host=host,
            probed=True,
            detail=f"{role}: {host}:{port} reachable.",
        )

    return ProviderProbe(
        role=role,
        provider=provider.name,
        env_var=env_var,
        status=degradation.canonical_status(_NATIVE_UNREACHABLE),
        check=CHECK_REACHABILITY,
        endpoint_host=host,
        probed=True,
        detail=(
            f"{role}: {env_var}={provider.name} endpoint {host}:{port} is not "
            f"reachable ({reason})."
        ),
    )


def evaluate_startup_posture(
    generation: ModelProvider,
    embedding: ModelProvider,
    generation_env_var: str,
    embedding_env_var: str,
    *,
    timeout: Optional[float] = None,
    enabled: Optional[bool] = None,
    connect: ConnectFn = tcp_connect,
) -> ProviderPosture:
    """Probe both roles independently and record the result. Never raises.

    Enforcement is a separate step (:func:`enforce_startup_posture`) so the
    posture is recorded for HP-2.5 even in the profile where it also blocks boot.
    """
    cache: Dict[Tuple[str, int], Optional[str]] = {}
    roles = [
        probe_role(
            ROLE_GENERATION,
            generation,
            generation_env_var,
            timeout=timeout,
            enabled=enabled,
            connect=connect,
            connect_cache=cache,
        ),
        probe_role(
            ROLE_EMBEDDING,
            embedding,
            embedding_env_var,
            timeout=timeout,
            enabled=enabled,
            connect=connect,
            connect_cache=cache,
        ),
    ]
    posture = ProviderPosture(roles=roles)
    _record(posture)
    return posture


def enforce_startup_posture(posture: ProviderPosture) -> None:
    """Log the posture, and refuse to boot when the profile has no fallback.

    Under ``customer_hosted`` an unhealthy role raises. ``unknown`` is
    deliberately EXEMPT: it means the posture was not established (probing
    disabled, or a provider that declares nothing to probe), and refusing to boot
    over an unmeasured provider would turn "we did not look" into "it is broken".

    Under ``saas`` nothing raises — a transient hosted-API blip must not stop the
    service coming up. The condition is logged and left on the posture for
    HP-2.5's health surface.
    """
    for role in posture.roles:
        if role.healthy:
            logger.info("model provider posture: %s", role.detail)
        elif role.status == degradation.STATUS_UNKNOWN:
            logger.debug("model provider posture: %s", role.detail)
        else:
            logger.warning("model provider posture: %s", role.detail)

    blocking = [
        r
        for r in posture.unhealthy_roles()
        if r.status != degradation.STATUS_UNKNOWN
    ]
    if not blocking:
        return

    if not is_customer_hosted():
        logger.warning(
            "model provider posture is %s under the saas profile — startup "
            "continues, and /api/health reports it. %d of %d roles unhealthy.",
            posture.status,
            len(blocking),
            len(posture.roles),
        )
        return

    details = " ".join(r.detail for r in blocking)
    message = (
        "Model provider startup posture check failed under "
        f"DEPLOYMENT_PROFILE=customer_hosted: {details} "
        "This deployment has no provider fallback, so it cannot run discovery "
        "with AI narrative or retrieval evidence until this is fixed."
    )
    # Offer the escape hatch ONLY when it would actually help. Suggesting
    # ENV_PROBE_TIMEOUT=0 for an unconfigured endpoint or a missing credential
    # would send an operator to a setting that changes nothing there — those
    # checks are free and run regardless of whether probing is enabled. A remedy
    # that does not work is worse than none: it spends the operator's time and
    # discredits the rest of the message.
    if any(r.check == CHECK_REACHABILITY for r in blocking):
        message += (
            f" If this host is deliberately unreachable at boot, set "
            f"{ENV_PROBE_TIMEOUT}=0 to skip reachability probing."
        )
    raise ProviderUnreachable(message)


__all__ = [
    "CHECK_CREDENTIAL",
    "CHECK_ENDPOINT_CONFIG",
    "CHECK_NOT_RUN",
    "CHECK_REACHABILITY",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "ENV_DISABLE",
    "ENV_PROBE_TIMEOUT",
    "ProviderPosture",
    "ProviderProbe",
    "ProviderUnreachable",
    "evaluate_startup_posture",
    "enforce_startup_posture",
    "probe_role",
    "probe_timeout_seconds",
    "probes_enabled",
    "provider_posture",
    "reset_posture",
    "split_endpoint",
    "tcp_connect",
]
