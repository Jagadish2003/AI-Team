"""
azure_app_insights.py — 2.0-D3 T1: the BOUNDED Application Insights read scope.

D3's framing (story context): "Application Insights is an Azure resource on
MSP-B2's rails, and its operationally relevant output (availability/failure
alerts, dependency failures, health events) maps into MSP-B0's operational-event
schema. This story is a mapper plus a bounded read path — not a new connector
family."

This module is the **read path**. It is deliberately NOT a connector: it adds no
client, no credential type, no ARM surface and no endpoint of its own. It applies
a scope decision to records the MSP-B2 Azure connector (``azure_events.py``)
ALREADY fetches from surfaces it ALREADY reaches:

  * Azure Monitor **Alerts** (``Microsoft.AlertsManagement/alerts``), and
  * Azure **Resource/Service Health events** (``Microsoft.ResourceHealth/events``).

That constraint is the task's, not an implementation convenience: "Application
Insights alerts and health/failure events **via Azure Monitor surfaces already
reached by B2**". Adding a new read surface here would be the very "new connector
family" the story rules out, so :data:`ALLOWED_ARM_PATHS` enumerates the paths
D3 may read and a structural test pins the Azure ingest layer to exactly that set.

Where the rules live (2.0-D3 T2 refactor)
-----------------------------------------
The *classification* rules — what counts as an App Insights signal, which of the
four kinds it is, and what is excluded telemetry — moved to
``discovery/signals/app_insights_signal.py`` when T2 added the B0 mapper, because
the mapper needs the identical answers and is invoked standalone (a mapper gets
only the raw record). They are RE-EXPORTED here unchanged, so this module remains
the ingest-side entry point and there is exactly ONE implementation — the same
"one implementation, re-exported" shape ``security_ops_common`` uses for
``reachable_within``, adopted after three drifted copies of the CI-dependency
traversal. A structural test asserts both modules resolve to the SAME function
objects, so a re-introduced private copy fails the build.

What remains genuinely this module's own:

  * :data:`ALLOWED_ARM_PATHS` — the ARM paths D3 permits,
  * :data:`EXCLUDED_ENDPOINT_MARKERS` and :func:`assert_read_allowed` — the
    call-time refusal of an out-of-scope surface,
  * :class:`AppInsightsScopeViolation`.

**What is excluded, and why it is enforced in code.** D3 must not turn AgentIQ
into a telemetry store or an observability platform. So raw Application Insights
telemetry — requests, exceptions, page views, traces, dependency calls, metric
samples, transactions, profiler data — and Log Analytics / KQL analytics results
are out of scope. Two independent guards, because a comment is not an enforcement:

  * :func:`is_excluded_telemetry` recognises telemetry/analytics record SHAPES, so
    a telemetry payload seeded into a stream is dropped before it can be mapped
    (D3-AC2). It is deliberately conservative — it fires only on unambiguous
    envelope markers, and short-circuits on the alert/activity/health envelopes, so
    it can never swallow a legitimate B2 record.
  * :func:`assert_read_allowed` refuses a URL that targets an excluded surface, so
    the exclusion also holds for anything that tries to CALL such an endpoint
    rather than merely hand us its output.

Transport-only, like every other value this connector adds: the scope is attached
to the record WRAPPER (never to the MSP-B0 event), so no provider-specific
detector field is invented. Event-level normalisation (resource = the monitored
application, class = alarm/health, deterministic signature) is D3 T2's B0 mapper
``reference_mappers.map_app_insights``; associating the application with a .NET
component or CMDB CI is T3.
"""
from __future__ import annotations

import logging

# ── the shared classification core (2.0-D3 T2) ──────────────────────────────────
# Re-exported, never re-implemented. Import order is deliberate: this module owns
# only the transport-level guards below.
from ..signals.app_insights_signal import (  # noqa: F401  (public re-exports)
    APP_INSIGHTS_EVENT_TYPE_UNCLASSIFIED,
    APP_INSIGHTS_EVENT_TYPES,
    APP_INSIGHTS_RESOURCE_TYPE,
    APP_INSIGHTS_SIGNAL_KINDS,
    APP_INSIGHTS_SURFACES,
    COMPONENT_SEGMENT,
    EXCLUDED_TELEMETRY_TYPES,
    FAILURE_SIGNAL_KINDS,
    MONITORED_APPLICATION_FIELDS,
    RESOLVED_MONITOR_CONDITIONS,
    SIGNAL_APPLICATION_FAILURE,
    SIGNAL_AVAILABILITY,
    SIGNAL_DEPENDENCY_FAILURE,
    SIGNAL_HEALTH_TRANSITION,
    SURFACE_AZURE_MONITOR,
    SURFACE_AZURE_SERVICE_HEALTH,
    AppInsightsScope,
    alert_declaration_text,
    app_insights_event_type,
    app_insights_scope,
    classify_alert_signal,
    classify_health_signal,
    component_id_from_resource_id,
    component_name_from_resource_id,
    detect_surface,
    is_alert_condition_active,
    is_excluded_telemetry,
    monitored_application_reference,
    referenced_component,
    referenced_resource_ids,
)

logger = logging.getLogger(__name__)


# ── the surfaces D3 is allowed to read (all already reached by MSP-B2) ───────────

#: The ARM paths the Azure ingest layer may read, after D3. EXPLICIT enumeration
#: (the same discipline as ``connector_roadmap.SHIPPED_CONNECTOR_IDS``) so the
#: scope-defence test asserts a set rather than trusting a docstring: D3 must add
#: no surface, and in particular no metrics, Log Analytics, or App Insights REST
#: telemetry path. Lower-cased; compared as substrings of a request path.
ALLOWED_ARM_PATHS = frozenset({
    "providers/microsoft.alertsmanagement/alerts",
    "providers/microsoft.insights/eventtypes/management/values",
    "providers/microsoft.resourcehealth/events",
})


# ── the exclusion list (D3-AC2 scope defence) ───────────────────────────────────

#: Substrings that mark a URL as an excluded surface: the App Insights REST
#: telemetry/analytics API, Log Analytics, KQL query endpoints, ARM metrics, and
#: profiler/snapshot surfaces. Checked case-insensitively.
EXCLUDED_ENDPOINT_MARKERS = frozenset({
    "api.applicationinsights.io",
    "api.loganalytics.io",
    "/v1/apps/",
    "microsoft.operationalinsights",
    "microsoft.insights/metrics",
    "microsoft.insights/logs",
    "/providers/microsoft.insights/metricdefinitions",
    "/query",
    "kusto",
    "/analytics",
    "/profiler",
    "/snapshot",
    "/transactions",
})


class AppInsightsScopeViolation(RuntimeError):
    """Raised when a read is attempted against an out-of-scope surface.

    Deliberately an exception rather than a logged warning: D3's scope boundary is
    a product commitment ("D3 must not turn AgentIQ into a telemetry storage or
    observability platform"), and a commitment that degrades to a log line is one
    a future change can cross without anyone noticing.
    """


def assert_read_allowed(url: str) -> None:
    """Refuse a URL that targets an excluded telemetry/analytics surface.

    The second of the two AC2 guards: :func:`is_excluded_telemetry` stops excluded
    DATA from being ingested, this stops an excluded ENDPOINT from being called at
    all. Applied to the live request path, so the boundary holds even for a caller
    that never hands us a record.
    """
    low = str(url or "").lower()
    for marker in EXCLUDED_ENDPOINT_MARKERS:
        if marker in low:
            raise AppInsightsScopeViolation(
                f"2.0-D3 scope: refusing to read {marker!r} — Application Insights "
                f"raw telemetry, metrics, transactions and Log Analytics/KQL "
                f"analytics are out of scope for AgentIQ"
            )


def is_allowed_arm_path(path: str) -> bool:
    """Whether ``path`` is one of the ARM paths D3 permits (see ALLOWED_ARM_PATHS)."""
    low = str(path or "").lower()
    return any(allowed in low for allowed in ALLOWED_ARM_PATHS)
