"""MSP-B0 / AT-635 — the Operational Event Schema: one model for every cloud.

Every MSP cloud-event source — AWS (MSP-B1), Azure (MSP-B2), and the Event
History export bridge (MSP-B8) — emits a provider-specific payload. This module
defines the single normalised shape they all map onto BEFORE the signal reaches
a detector, so detectors, scoring, corroboration, and reporting never see a
provider-specific event. This is the contract everything else in the MSP pack
implements; MSP-B0 is sequenced first because B1, B2, and B8 all target it
(T1-AC3).

A *profile* of the common signal model
---------------------------------------
The schema is deliberately built as a **profile** — a constrained
specialisation — of AgentIQ's existing signal foundations rather than a brand-new
parallel model:

* :class:`CommonSignal` is the shared spine every AgentIQ signal carries: the
  owning ``org_id`` (tenancy scoping — every signal is org-scoped, per
  ``middleware/tenancy.py``), the source-system id, a stable per-source
  ``signal_id``, an observation timestamp, and a fully-populated OBSERVED
  :class:`~app.provenance.EvidencePointer` (the R16-B1 provenance spine reused
  verbatim — cloud events are directly measured, so they are first-class
  observed evidence, never inferred).
* :class:`OperationalEvent` is the operational-event **profile** of that spine:
  it adds the normalised operational vocabulary (``resource_type``,
  ``event_class``, ``severity`` — T1-AC1) plus the resource the event concerns
  (:class:`ResourceRef`) and the provider-native ``event_type`` preserved for
  trace-back.

Normalised vocabularies (T1-AC1)
--------------------------------
``resource_type``, ``event_class``, and ``severity`` are closed vocabularies —
a value outside the frozen set fails loudly at construction rather than flowing
downstream as an unrecognised token that a detector would silently mis-handle.
Each provider's connector maps its native taxonomy onto these tokens via the
``normalize_*`` helpers, so a detector reasons about ``severity == "critical"``
without caring whether AWS said ``"CRITICAL"``, Azure said ``"Sev0"``, or the
bridge said ``"error"``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

try:
    from app.provenance import EvidencePointer, utc_now_iso
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.provenance import EvidencePointer, utc_now_iso

from .event_signature import compute_event_signature


# ─────────────────────────────────────────────────────────────────────────────
# Normalised vocabularies (T1-AC1)
# ─────────────────────────────────────────────────────────────────────────────
# These are CLOSED sets. A cloud connector must map its provider-native taxonomy
# onto one of these tokens before emitting an OperationalEvent; an unrecognised
# value fails at construction (see OperationalEvent.__post_init__) so a typo or
# an unmapped provider category surfaces at the source instead of silently
# reaching a detector.

#: Normalised resource categories. Cloud services are grouped by what they *are*
#: to a detector, not by their provider-specific product name (e.g. both AWS EC2
#: and Azure Virtual Machines normalise to ``"compute"``).
RESOURCE_TYPES: frozenset = frozenset({
    "compute",       # VMs, instances, app services
    "container",     # ECS/EKS/AKS, container instances
    "serverless",    # Lambda, Azure Functions
    "storage",       # object/blob/file storage, disks
    "database",      # managed relational / NoSQL / cache
    "network",       # VPC/VNet, load balancers, gateways, DNS
    "identity",      # IAM, Entra, roles, policies, keys
    "messaging",     # queues, topics, event buses
    "monitoring",    # metrics, logs, alarms, health
    "security",      # guard/defender, config rules, findings
    "other",         # a real resource that does not fit the above
})

#: Normalised event categories — what *kind* of thing happened, independent of
#: the resource it happened to.
EVENT_CLASSES: frozenset = frozenset({
    "lifecycle",       # create / delete / start / stop
    "configuration",   # a setting / policy / tag changed
    "state_change",    # health / running-state transition
    "access",          # authn / authz / API call on a resource
    "error",           # a failed operation / fault
    "performance",     # latency / throughput / saturation signal
    "security",        # a security-relevant finding or alert
    "audit",           # a governance / compliance audit record
    "other",           # a real event that does not fit the above
})

#: Normalised severity ladder, highest to lowest. Ordered so callers can compare
#: relative severity via :data:`SEVERITY_ORDER`.
SEVERITY_LEVELS: frozenset = frozenset({
    "critical",
    "high",
    "medium",
    "low",
    "info",
})

#: Rank for each severity (higher number = more severe) so downstream code can
#: threshold/compare without re-deriving the ordering from the frozenset.
SEVERITY_ORDER: Dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}

# Provider-native synonym maps consulted by the normalise helpers. These cover
# the common AWS / Azure / export-bridge spellings; a value already canonical
# passes straight through. Kept intentionally small — a connector with an exotic
# taxonomy extends its own map before handing events to the schema.
_SEVERITY_SYNONYMS: Dict[str, str] = {
    # AWS / generic
    "critical": "critical", "crit": "critical", "fatal": "critical",
    "sev0": "critical", "sev1": "critical", "p1": "critical",
    "error": "high", "high": "high", "sev2": "high", "p2": "high",
    "warning": "medium", "warn": "medium", "medium": "medium",
    "moderate": "medium", "sev3": "medium", "p3": "medium",
    "low": "low", "minor": "low", "sev4": "low", "p4": "low",
    "info": "info", "informational": "info", "notice": "info",
    "verbose": "info", "debug": "info",
}
_EVENT_CLASS_SYNONYMS: Dict[str, str] = {
    "create": "lifecycle", "delete": "lifecycle", "start": "lifecycle",
    "stop": "lifecycle", "terminate": "lifecycle", "provision": "lifecycle",
    "update": "configuration", "modify": "configuration", "config": "configuration",
    "write": "configuration", "tag": "configuration",
    "status": "state_change", "health": "state_change", "transition": "state_change",
    "login": "access", "authn": "access", "authz": "access", "apicall": "access",
    "fault": "error", "failure": "error", "failed": "error", "exception": "error",
    "latency": "performance", "throughput": "performance", "saturation": "performance",
    "throttle": "performance",
    "alert": "security", "finding": "security", "threat": "security",
    "compliance": "audit", "governance": "audit",
}
_RESOURCE_TYPE_SYNONYMS: Dict[str, str] = {
    "vm": "compute", "instance": "compute", "ec2": "compute",
    "virtualmachine": "compute", "appservice": "compute", "server": "compute",
    "function": "serverless", "lambda": "serverless",
    "ecs": "container", "eks": "container", "aks": "container",
    "containerinstance": "container", "pod": "container",
    "s3": "storage", "blob": "storage", "bucket": "storage", "disk": "storage",
    "volume": "storage", "fileshare": "storage",
    "rds": "database", "sql": "database", "cosmos": "database",
    "dynamodb": "database", "cache": "database", "redis": "database",
    "vpc": "network", "vnet": "network", "loadbalancer": "network",
    "gateway": "network", "dns": "network", "subnet": "network",
    "iam": "identity", "entra": "identity", "role": "identity",
    "policy": "identity", "keyvault": "identity", "key": "identity",
    "queue": "messaging", "topic": "messaging", "eventbus": "messaging",
    "sns": "messaging", "sqs": "messaging", "servicebus": "messaging",
    "metric": "monitoring", "log": "monitoring", "alarm": "monitoring",
    "guardduty": "security", "defender": "security", "securityhub": "security",
}


def _normalize(value: Optional[str], synonyms: Dict[str, str],
               canonical: frozenset, default: str) -> str:
    """Map a raw provider token onto the canonical vocabulary.

    Already-canonical values pass through; recognised synonyms are mapped; an
    empty or unrecognised value falls back to ``default`` (``"other"`` / ``"info"``)
    so a source that omits or misspells a field still yields a valid event rather
    than raising. Case- and separator-insensitive (``"Sev 0"`` == ``"sev0"``).
    """
    if not value:
        return default
    key = "".join(ch for ch in str(value).strip().lower() if ch.isalnum())
    if key in canonical:
        return key
    return synonyms.get(key, default)


def normalize_resource_type(value: Optional[str]) -> str:
    """Normalise a provider-native resource label to a :data:`RESOURCE_TYPES` token."""
    return _normalize(value, _RESOURCE_TYPE_SYNONYMS, RESOURCE_TYPES, "other")


def normalize_event_class(value: Optional[str]) -> str:
    """Normalise a provider-native event label to an :data:`EVENT_CLASSES` token."""
    return _normalize(value, _EVENT_CLASS_SYNONYMS, EVENT_CLASSES, "other")


def normalize_severity(value: Optional[str]) -> str:
    """Normalise a provider-native severity label to a :data:`SEVERITY_LEVELS` token."""
    return _normalize(value, _SEVERITY_SYNONYMS, SEVERITY_LEVELS, "info")


# ─────────────────────────────────────────────────────────────────────────────
# Resource reference model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResourceRef:
    """A normalised reference to the cloud resource an event concerns.

    Providers name resources differently (an AWS ARN, an Azure resource id), so
    the reference keeps the ``provider`` and the provider-native ``resource_id``
    verbatim for trace-back while exposing the normalised ``resource_type``
    (:data:`RESOURCE_TYPES`) that detectors reason over. ``region`` and ``name``
    are optional display detail.
    """

    provider: str                         # 'aws' | 'azure' | 'event_bridge' | ...
    resource_type: str                    # normalised — must be in RESOURCE_TYPES
    resource_id: str                      # provider-native id (ARN / resource URI)
    region: Optional[str] = None
    name: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("ResourceRef.provider is required")
        if not self.resource_id:
            raise ValueError("ResourceRef.resource_id is required")
        if self.resource_type not in RESOURCE_TYPES:
            raise ValueError(
                f"resource_type must be one of {sorted(RESOURCE_TYPES)}, "
                f"got {self.resource_type!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable mapping for storage in a signal payload."""
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Common signal model (the spine the operational event profiles)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CommonSignal:
    """The shared spine every AgentIQ signal carries.

    A signal is always owned by an org (tenancy scoping), traces back to a
    source system, has a stable per-source id and an observation time, and
    carries a provenance pointer recording exactly where it came from. Concrete
    signal families (the operational event below, and future MSP signal
    profiles) specialise this spine rather than reinventing source tracking.
    """

    org_id: str                           # tenancy scoping — every signal is org-scoped
    source_system: str                    # 'aws' | 'azure' | 'event_bridge' | ...
    signal_id: str                        # stable id within the source system
    observed_at: str                      # UTC ISO-8601 observation time
    provenance: Dict[str, Any]            # EvidencePointer.to_dict() (OBSERVED spine)

    def __post_init__(self) -> None:
        if not self.org_id:
            raise ValueError("org_id is required — every signal is org-scoped")
        if not self.source_system:
            raise ValueError("source_system is required")
        if not self.signal_id:
            raise ValueError("signal_id is required")
        if not self.observed_at:
            raise ValueError("observed_at is required")
        # Provenance must be a populated, valid OBSERVED spine — a signal with no
        # traceable origin is not persistable (R16-B1 AC2). A malformed / partial
        # mapping (e.g. an empty dict missing spine fields) is treated as invalid
        # rather than allowed to raise a low-level TypeError from from_dict().
        try:
            valid = (
                isinstance(self.provenance, dict)
                and EvidencePointer.from_dict(self.provenance).is_valid()
            )
        except TypeError:
            valid = False
        if not valid:
            raise ValueError("provenance must be a valid EvidencePointer spine")


# ─────────────────────────────────────────────────────────────────────────────
# Operational event profile (T1-AC1 / T1-AC3)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OperationalEvent(CommonSignal):
    """The normalised operational event every cloud source emits against.

    A profile of :class:`CommonSignal` that adds the operational vocabulary:
    the normalised ``resource_type`` / ``event_class`` / ``severity`` a detector
    reasons over (T1-AC1), the :class:`ResourceRef` the event concerns, the
    provider-native ``event_type`` preserved for trace-back, and an optional
    human-readable ``message``. The free-form ``payload`` carries any
    provider-specific detail a detector may want without leaking a
    provider-specific *shape* into the common contract (T1-AC3).

    Construct directly when the caller already holds canonical values, or via
    :meth:`build` to normalise provider-native tokens and mint the OBSERVED
    provenance pointer in one step — the path a cloud connector uses.
    """

    resource_type: str = "other"          # normalised — must be in RESOURCE_TYPES
    event_class: str = "other"            # normalised — must be in EVENT_CLASSES
    severity: str = "info"                # normalised — must be in SEVERITY_LEVELS
    event_type: str = ""                  # provider-native event name (trace-back)
    resource: Optional[ResourceRef] = None
    message: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    # Deterministic recurrence fingerprint (MSP-B0 / AT-636). Auto-derived from
    # the event's identity in __post_init__ when not supplied; the same recurring
    # event always yields the same signature. See event_signature.py.
    event_signature: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.resource_type not in RESOURCE_TYPES:
            raise ValueError(
                f"resource_type must be one of {sorted(RESOURCE_TYPES)}, "
                f"got {self.resource_type!r}"
            )
        if self.event_class not in EVENT_CLASSES:
            raise ValueError(
                f"event_class must be one of {sorted(EVENT_CLASSES)}, "
                f"got {self.event_class!r}"
            )
        if self.severity not in SEVERITY_LEVELS:
            raise ValueError(
                f"severity must be one of {sorted(SEVERITY_LEVELS)}, "
                f"got {self.severity!r}"
            )
        if self.resource is not None and not isinstance(self.resource, ResourceRef):
            raise ValueError("resource must be a ResourceRef or None")
        # Derive the deterministic recurrence fingerprint from the event's
        # identity unless the caller supplied one (AT-636). Recomputed here so a
        # directly-constructed event carries a signature just like a built one.
        if not self.event_signature:
            self.event_signature = compute_event_signature(
                source_system=self.source_system,
                event_class=self.event_class,
                resource_type=self.resource_type,
                event_type=self.event_type,
                resource_id=self.resource.resource_id if self.resource else None,
                principal=self._principal(),
            )

    def _principal(self) -> Optional[str]:
        """The acting principal for actor-sensitive event classes.

        Read from the free-form ``payload`` under the common keys a provider may
        use. Only the access/audit/security recipes consult it, but resolving it
        uniformly keeps the signature call site simple.
        """
        for key in ("principal", "actor", "user", "user_identity", "caller"):
            val = self.payload.get(key)
            if val:
                return str(val)
        return None

    @property
    def severity_rank(self) -> int:
        """Numeric severity rank (higher = more severe) for thresholding/sorting."""
        return SEVERITY_ORDER[self.severity]

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable mapping — the normalised shape stored/forwarded downstream."""
        data = asdict(self)
        # asdict already recursed into the ResourceRef dataclass; keep None as-is.
        return data

    @classmethod
    def build(
        cls,
        *,
        org_id: str,
        source_system: str,
        signal_id: str,
        event_type: str,
        resource: Optional[ResourceRef] = None,
        resource_type: Optional[str] = None,
        event_class: Optional[str] = None,
        severity: Optional[str] = None,
        observed_at: Optional[str] = None,
        message: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> "OperationalEvent":
        """Build a normalised OperationalEvent from provider-native inputs.

        The path a cloud connector (AWS/Azure/bridge) uses: raw
        ``resource_type`` / ``event_class`` / ``severity`` tokens are run through
        the ``normalize_*`` helpers, ``observed_at`` defaults to now (UTC), and an
        OBSERVED :class:`~app.provenance.EvidencePointer` is minted when the caller
        does not supply one (cloud events are directly measured — always observed,
        never inferred, so no ``extraction_job_id`` is required). When ``resource``
        is given and ``resource_type`` is omitted, the resource's own type is used.
        """
        observed_at = observed_at or utc_now_iso()
        raw_resource_type = resource_type or (resource.resource_type if resource else None)
        if provenance is None:
            provenance = EvidencePointer.observed(
                source_system=source_system,
                source_artifact=signal_id,
                source_timestamp=observed_at,
                source_artifact_type="record_id",
            ).to_dict()
        return cls(
            org_id=org_id,
            source_system=source_system,
            signal_id=signal_id,
            observed_at=observed_at,
            provenance=provenance,
            resource_type=normalize_resource_type(raw_resource_type),
            event_class=normalize_event_class(event_class),
            severity=normalize_severity(severity),
            event_type=event_type,
            resource=resource,
            message=message,
            payload=dict(payload or {}),
        )
