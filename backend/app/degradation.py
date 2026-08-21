"""2.0-D4 T5 — one shape for "part of this did not work" (AC6).

**The failure this exists to prevent** is not a crash. It is a run that
completes, produces findings, and looks entirely normal while silently missing a
source. A user who does not know their ServiceNow data was absent reads the
findings as complete, and every downstream decision inherits that error. In an
evidence platform that is the worst failure mode there is, because nothing
appears wrong.

**Why one module rather than three fixes.** The platform already degrades well in
places — the AWS connector tracks per-account health and keeps other accounts
running, ServiceNow distinguishes an unactivated plugin from a transport error —
but each grew its own vocabulary:

===========================  ==========================================
Source                       Vocabulary it grew
===========================  ==========================================
``aws_health``               ``ok`` / ``auth_failed`` / ``partial`` / ``failed``
ServiceNow SecOps tables     ``unavailable`` + named reason
run stages                   ``degraded``
operational apps             ``credential_missing``
model gateway                ``ok=False`` (no status at all)
retrieval store              raises (deliberately — see below)
===========================  ==========================================

A run-health consumer currently has to special-case each one. This module
defines the canonical set they all map onto, so a surface can render any
degradation without knowing which subsystem produced it — and so that a NEW
source cannot invent a sixth vocabulary without deciding where it fits.

**The mapping is lossless in the direction that matters.** Converging does not
discard the source's own word: every component keeps its native status in
``native_status`` and gains a canonical one. ``auth_failed`` and
``credential_missing`` both canonicalise to ``UNAVAILABLE`` because a customer
sees the same thing (no data from that source, and a credential is why), while
the native value still tells an engineer which code path produced it.

**The posture this generalises** comes from R18-B2's freshness metrics, which
refuse to degrade to zeros on a read failure because "0 stale chunks" would
report perfect freshness while the store is down. Generalised here as: *never
report a healthy-looking number derived from an unhealthy source.* A component
that could not be measured reports ``UNKNOWN``, never ``ok``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEGRADATION_SCHEMA_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# The canonical status set
# --------------------------------------------------------------------------

#: Everything this component was asked to do, it did.
STATUS_OK = "ok"
#: The component worked, but not for everything — some scopes/records/accounts
#: succeeded and some did not. The customer got a real but incomplete answer.
STATUS_PARTIAL = "partial"
#: The component produced nothing, for a reason that is not an error: a plugin
#: is not activated, a credential is absent, a provider is not configured.
#: Actionable by the customer, and NOT a bug.
STATUS_UNAVAILABLE = "unavailable"
#: The component was reachable and tried, and failed. Actionable by whoever
#: operates it; usually a bug or an outage.
STATUS_FAILED = "failed"
#: The component's health could not be established. Deliberately NOT ``ok`` —
#: reporting an unmeasured component as healthy is the exact failure the
#: freshness-metrics posture forbids.
STATUS_UNKNOWN = "unknown"

CANONICAL_STATUSES: Tuple[str, ...] = (
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    STATUS_FAILED,
    STATUS_UNKNOWN,
)

#: Ordered worst-first. Used to roll several components up into one verdict:
#: the run is as bad as its worst component, never as good as its best.
_SEVERITY: Dict[str, int] = {
    STATUS_FAILED: 4,
    STATUS_UNAVAILABLE: 3,
    STATUS_PARTIAL: 2,
    STATUS_UNKNOWN: 1,
    STATUS_OK: 0,
}

#: Every native vocabulary in the codebase, mapped onto the canonical set.
#: Adding a source means adding its words here — which is the point: a new
#: vocabulary cannot be introduced without deciding what a customer should see.
NATIVE_STATUS_MAP: Dict[str, str] = {
    # discovery/ingest/aws_health.py
    "ok": STATUS_OK,
    "partial": STATUS_PARTIAL,
    "auth_failed": STATUS_UNAVAILABLE,   # no data, and a credential is why
    "failed": STATUS_FAILED,
    # ServiceNow SecOps tables (plugin not activated / role missing)
    "unavailable": STATUS_UNAVAILABLE,
    # discovery/ingest/operational_config.py
    "credential_missing": STATUS_UNAVAILABLE,
    # app/model_gateway/probe.py (HP-2.3) — a configured model endpoint we could
    # not open a connection to. UNAVAILABLE rather than FAILED: nothing was
    # produced and the cause is the deployment's own network/config reach, which
    # the customer can act on — the same reading as auth_failed/not_configured.
    # FAILED is reserved for "we got through, and it broke".
    "unreachable": STATUS_UNAVAILABLE,
    # The reachability probe did not run (disabled, or suppressed under test
    # isolation). NOT ok — an unmeasured provider is UNKNOWN by this module's
    # standing rule.
    "not_probed": STATUS_UNKNOWN,
    # run stages
    "degraded": STATUS_PARTIAL,
    "complete": STATUS_OK,
    "success": STATUS_OK,
    "succeeded": STATUS_OK,
    "error": STATUS_FAILED,
    "skipped": STATUS_UNAVAILABLE,
    "not_configured": STATUS_UNAVAILABLE,
    "unknown": STATUS_UNKNOWN,
}


def canonical_status(native: Optional[str]) -> str:
    """Map a subsystem's own word onto the canonical set.

    An UNRECOGNISED status becomes ``UNKNOWN``, never ``ok``. A word this module
    has not been taught is not evidence of health — treating it as healthy is
    how a new failure mode ships looking fine.
    """
    if not native:
        return STATUS_UNKNOWN
    return NATIVE_STATUS_MAP.get(str(native).strip().lower(), STATUS_UNKNOWN)


def worst(statuses: Iterable[str]) -> str:
    """The worst of several statuses. An empty set is UNKNOWN, not ok."""
    ranked = [s for s in statuses if s in _SEVERITY]
    if not ranked:
        return STATUS_UNKNOWN
    return max(ranked, key=lambda s: _SEVERITY[s])


def is_healthy(status: str) -> bool:
    """Only ``ok`` is healthy. UNKNOWN is deliberately not."""
    return status == STATUS_OK


# --------------------------------------------------------------------------
# The uniform report
# --------------------------------------------------------------------------

#: The kinds of thing that can degrade. Named so a surface can group them.
COMPONENT_CONNECTOR = "connector"
COMPONENT_MODEL = "model"
COMPONENT_STORAGE = "storage"
COMPONENT_STAGE = "stage"

COMPONENT_KINDS: Tuple[str, ...] = (
    COMPONENT_CONNECTOR,
    COMPONENT_MODEL,
    COMPONENT_STORAGE,
    COMPONENT_STAGE,
)


@dataclass(frozen=True)
class ComponentDegradation:
    """One thing that did not fully work, in the shape every surface expects.

    The four fields the subtask asks for — what was attempted, what succeeded,
    what did not, and a named actionable reason — are mandatory rather than
    optional, because a degradation report missing any of them cannot be acted
    on and will be ignored.
    """

    kind: str
    #: Stable id of the thing: 'servicenow', 'embedding_provider', 'retrieval_store'.
    component: str
    status: str
    #: The subsystem's own word, preserved so an engineer can find the code path.
    native_status: Optional[str] = None
    #: What this component was asked to do.
    attempted: Optional[str] = None
    #: What the customer still got from it. "" / None means nothing.
    delivered: Optional[str] = None
    #: What they did not get. The sentence that stops a partial run reading clean.
    missing: Optional[str] = None
    #: Why, in words a customer can act on.
    reason: Optional[str] = None
    #: What to do about it.
    remedy: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return is_healthy(self.status)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "component": self.component,
            "status": self.status,
            "nativeStatus": self.native_status,
            "attempted": self.attempted,
            "delivered": self.delivered,
            "missing": self.missing,
            "reason": self.reason,
            "remedy": self.remedy,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class RunCompleteness:
    """Whether a run delivered everything it set out to, and what it did not.

    This is the ONE fact every surface reads. The opportunity list, the roadmap,
    the executive report and the run-health panel must all derive their
    completeness wording from here rather than each deciding for itself — that
    is what stops one surface saying "complete" while another says "partial".
    """

    run_id: Optional[str]
    status: str
    components: Tuple[ComponentDegradation, ...] = ()

    @property
    def complete(self) -> bool:
        return self.status == STATUS_OK

    @property
    def degraded_components(self) -> Tuple[ComponentDegradation, ...]:
        return tuple(c for c in self.components if not c.healthy)

    @property
    def headline(self) -> str:
        """The sentence a surface shows. Composed here so none composes its own."""
        bad = self.degraded_components
        if not bad:
            return "All configured sources and services reported healthy for this run."
        names = ", ".join(sorted({c.component for c in bad}))
        return (
            f"This run is INCOMPLETE: {len(bad)} component(s) did not fully "
            f"succeed ({names}). Findings below are drawn only from the sources "
            "that did report — treat them as partial."
        )

    @property
    def missing_summary(self) -> List[str]:
        """One line per thing the customer did not get."""
        return [
            f"{c.component}: {c.missing or c.reason or 'no data'}"
            for c in self.degraded_components
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": DEGRADATION_SCHEMA_VERSION,
            "runId": self.run_id,
            "status": self.status,
            "complete": self.complete,
            "headline": self.headline,
            "missing": self.missing_summary,
            "degradedCount": len(self.degraded_components),
            "components": [c.to_dict() for c in self.components],
        }


__all__ = [
    "CANONICAL_STATUSES",
    "COMPONENT_CONNECTOR",
    "COMPONENT_KINDS",
    "COMPONENT_MODEL",
    "COMPONENT_STAGE",
    "COMPONENT_STORAGE",
    "DEGRADATION_SCHEMA_VERSION",
    "NATIVE_STATUS_MAP",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_UNAVAILABLE",
    "STATUS_UNKNOWN",
    "ComponentDegradation",
    "RunCompleteness",
    "canonical_status",
    "is_healthy",
    "worst",
]
