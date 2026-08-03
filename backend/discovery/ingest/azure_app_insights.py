"""
azure_app_insights.py — 2.0-D3 T1: the BOUNDED Application Insights read scope.

D3's framing (story context): "Application Insights is an Azure resource on
MSP-B2's rails, and its operationally relevant output (availability/failure
alerts, dependency failures, health events) maps into MSP-B0's operational-event
schema. This story is a mapper plus a bounded read path — not a new connector
family."

This module is the **read path**. It is deliberately NOT a connector: it adds no
client, no credential type, no ARM surface and no endpoint of its own. It is a
pure classification + scope-defence layer applied to records the MSP-B2 Azure
connector (``azure_events.py``) ALREADY fetches from surfaces it ALREADY reaches:

  * Azure Monitor **Alerts** (``Microsoft.AlertsManagement/alerts``), and
  * Azure **Resource/Service Health events** (``Microsoft.ResourceHealth/events``).

That constraint is the task's, not an implementation convenience: "Application
Insights alerts and health/failure events **via Azure Monitor surfaces already
reached by B2**". Adding a new read surface here would be the very "new connector
family" the story rules out, so :data:`ALLOWED_ARM_PATHS` enumerates the paths
D3 may read and a structural test pins the Azure ingest layer to exactly that set.

Two things this module decides, and nothing else:

1. **Is this record in App Insights scope?** Only when it EXPLICITLY references an
   Application Insights component (``microsoft.insights/components``). There is no
   inference: no name matching, no resource-group heuristics, no "looks like an
   app" guessing. :func:`app_insights_scope` returns ``None`` otherwise, and a
   ``None`` scope changes nothing about how B2 already handles the record.

2. **Which operational signal is it?** One of the four kinds D3 names
   (:data:`APP_INSIGHTS_SIGNAL_KINDS`), decided from the record's DECLARED alert
   fields only (rule name, signal type, monitoring service, condition type, metric
   name) — never from telemetry content, because telemetry is not read at all. An
   in-scope record whose kind cannot be established keeps ``signal_kind=None``
   rather than being forced into a bucket; the same "ambiguous stays unresolved"
   discipline D3's AC3 applies to IIS association.

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
detector field is invented. Turning the scope into event-level normalisation
(resource = the monitored application, class = alarm/health, deterministic
signature) is D3 T2's B0 mapper, and associating the application with a .NET
component or CMDB CI is T3 — both build on this module's output.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── the Application Insights resource identity ──────────────────────────────────

#: The ARM resource type of an Application Insights component. An App Insights
#: "application" IS a resource of this type, which is what lets D3 ride B2's rails
#: instead of needing an App Insights-specific connection.
APP_INSIGHTS_RESOURCE_TYPE = "microsoft.insights/components"

#: The ARM id segment that EXPLICITLY marks an App Insights component. Matching is
#: on this full provider+type path (not merely "insights" or "components"), so an
#: unrelated ``microsoft.insights/*`` resource — a metric alert rule, an action
#: group, an autoscale setting — is never mistaken for a monitored application.
_COMPONENT_SEGMENT = "/providers/microsoft.insights/components/"


# ── the four operational signal kinds D3 names ──────────────────────────────────

#: An availability signal: the application is not answering (web/ping test).
SIGNAL_AVAILABILITY = "availability"
#: An application-failure signal: the application itself is erroring.
SIGNAL_APPLICATION_FAILURE = "application_failure"
#: A dependency-failure signal: something the application calls is failing. In
#: scope because it describes an OPERATIONAL FAILURE — which is not the same thing
#: as the individual dependency call records, which are telemetry and excluded.
SIGNAL_DEPENDENCY_FAILURE = "dependency_failure"
#: A health-transition signal: the monitored application's health state moved.
SIGNAL_HEALTH_TRANSITION = "health_transition"

#: Closed vocabulary. A kind outside this set is a bug, not a new feature.
APP_INSIGHTS_SIGNAL_KINDS = frozenset({
    SIGNAL_AVAILABILITY,
    SIGNAL_APPLICATION_FAILURE,
    SIGNAL_DEPENDENCY_FAILURE,
    SIGNAL_HEALTH_TRANSITION,
})


# ── the surfaces D3 is allowed to read (all already reached by MSP-B2) ───────────

#: MSP-B0 source systems of the two B2 surfaces D3 draws from. Used to decide which
#: classifier applies; D3 adds no third surface.
SURFACE_AZURE_MONITOR = "azure_monitor"
SURFACE_AZURE_SERVICE_HEALTH = "azure_service_health"

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

#: App Insights telemetry envelope ``baseType``/``itemType`` values. Every one of
#: these is per-transaction or per-sample telemetry, which D3 does not read.
EXCLUDED_TELEMETRY_TYPES = frozenset({
    "requestdata", "request", "requests",
    "exceptiondata", "exception", "exceptions",
    "remotedependencydata", "dependency", "dependencies",
    "pageviewdata", "pageview", "pageviews",
    "pageviewperformancedata",
    "messagedata", "trace", "traces",
    "metricdata", "metric", "metrics", "custommetric", "custommetrics",
    "eventdata", "customevent", "customevents",
    "availabilitydata", "availabilityresult", "availabilityresults",
    "performancecounter", "performancecounters",
    "browsertiming", "browsertimings",
    "transaction", "transactions",
    "profiler", "snapshot",
})

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


# ── record-shape helpers (read DECLARED fields only, never telemetry content) ────


def _essentials(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The common-alert-schema ``data.essentials`` block, or {}."""
    return ((raw or {}).get("data") or {}).get("essentials") or {}


def _alert_context(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The common-alert-schema ``data.alertContext`` block, or {}."""
    return ((raw or {}).get("data") or {}).get("alertContext") or {}


def _value_of(field: Any) -> str:
    """Unwrap Azure's ``{"value": x}`` envelope, tolerating a plain scalar.

    The Activity Log / health surfaces wrap several fields this way while the alert
    surface does not, so every field read goes through here (the same tolerance
    ``azure_admin_events`` applies).
    """
    if isinstance(field, dict):
        return str(field.get("value") or "")
    return str(field or "")


def _has_alert_envelope(raw: Dict[str, Any]) -> bool:
    """Whether the record is an Azure Monitor alert (has ``data.essentials``)."""
    return bool(_essentials(raw))


def _has_health_or_activity_envelope(raw: Dict[str, Any]) -> bool:
    """Whether the record is an Activity Log / health event record.

    Keyed on the fields those surfaces always carry. Used to short-circuit the
    telemetry check so a legitimate B2 record can never be mistaken for telemetry.
    """
    r = raw or {}
    return bool(r.get("eventDataId") or r.get("eventTimestamp") or r.get("operationName"))


# ── explicit component resolution (no inference — D3's rule) ─────────────────────


def component_id_from_resource_id(resource_id: Any) -> Optional[str]:
    """Return the App Insights component's ARM id, or ``None``.

    EXPLICIT match only: the id must contain ``/providers/microsoft.insights/
    components/<name>`` with a non-empty name. Case-insensitive (ARM ids are not
    case-normalised by Azure), and the returned value is the ORIGINAL-CASE id
    truncated at the component, so a child resource id
    (``.../components/app/providers/...``) resolves to its parent component rather
    than being rejected or carried whole.
    """
    rid = str(resource_id or "")
    if not rid:
        return None
    idx = rid.lower().find(_COMPONENT_SEGMENT)
    if idx == -1:
        return None
    start = idx + len(_COMPONENT_SEGMENT)
    name = rid[start:].split("/", 1)[0].strip()
    if not name:
        return None
    return rid[:start] + name


def component_name_from_resource_id(resource_id: Any) -> Optional[str]:
    """The App Insights component's short name, or ``None`` when not one."""
    cid = component_id_from_resource_id(resource_id)
    if cid is None:
        return None
    return cid.rsplit("/", 1)[-1] or None


def _referenced_resource_ids(raw: Dict[str, Any], *, surface: str) -> List[str]:
    """Every resource id the record EXPLICITLY names, in declaration order.

    Reads only documented reference fields:
      * alerts — ``essentials.alertTargetIDs``, then ``essentials.alertId`` (a
        target-scoped alert id can itself name the component),
      * health/activity — ``properties.impactedResources[].resourceId``,
        ``properties.resourceId``, ``resourceUri``, ``resourceId``.

    No other field is consulted, so nothing about the association is inferred.
    """
    out: List[str] = []
    if surface == SURFACE_AZURE_MONITOR:
        targets = _essentials(raw).get("alertTargetIDs") or []
        if isinstance(targets, list):
            out.extend(str(t) for t in targets if t)
        alert_id = _essentials(raw).get("alertId")
        if alert_id:
            out.append(str(alert_id))
        return out

    props = (raw or {}).get("properties") or {}
    impacted = props.get("impactedResources") or []
    if isinstance(impacted, list):
        for entry in impacted:
            if isinstance(entry, dict):
                rid = entry.get("resourceId") or entry.get("resourceUri") or entry.get("id")
                if rid:
                    out.append(str(rid))
            elif entry:
                out.append(str(entry))
    for key in ("resourceId", "resourceUri"):
        if props.get(key):
            out.append(str(props[key]))
        if (raw or {}).get(key):
            out.append(str(raw[key]))
    return out


def referenced_component(raw: Dict[str, Any], *, surface: str) -> Optional[str]:
    """The App Insights component this record explicitly references, or ``None``.

    The scope gate. Returns the FIRST explicitly-referenced component so a record
    naming several resources (a health event with many impacted resources) still
    resolves deterministically.
    """
    for rid in _referenced_resource_ids(raw, surface=surface):
        cid = component_id_from_resource_id(rid)
        if cid is not None:
            return cid
    return None


# ── signal-kind classification (declared alert fields only) ─────────────────────

#: Tokens that declare an AVAILABILITY signal. ``WebtestLocationAvailabilityCriteria``
#: is the condition type Azure stamps on a web/ping-test availability alert.
_AVAILABILITY_TOKENS = (
    "webtestlocationavailabilitycriteria",
    "availability",
    "webtest",
    "web test",
    "ping test",
)

#: Tokens that declare a DEPENDENCY-FAILURE signal. Checked BEFORE the generic
#: application-failure tokens, because "dependency call failures" is both a
#: dependency signal and a failure signal, and the more specific kind must win.
_DEPENDENCY_FAILURE_TOKENS = (
    "dependency",
    "dependencies",
    "dependencycall",
    "outbound call",
)

#: Tokens that declare an APPLICATION-FAILURE signal. ``failureanomalies`` is
#: App Insights Smart Detection's rule name for its failure-rate detector.
_APPLICATION_FAILURE_TOKENS = (
    "failureanomalies",
    "failure anomalies",
    "smartdetector",
    "smart detector",
    "failed request",
    "requests/failed",
    "failure rate",
    "server exception",
    "exception",
    "failure",
    "error",
)

#: Tokens that declare a HEALTH-TRANSITION signal on the health surface.
_HEALTH_TRANSITION_TOKENS = (
    "resourcehealth",
    "healthevent",
    "availabilitystatus",
    "servicehealth",
)


def _alert_declaration_text(raw: Dict[str, Any]) -> str:
    """The lower-cased concatenation of an alert's DECLARED descriptive fields.

    Exactly five fields, all part of the alert's own declaration of what it
    monitors: the rule name, the signal type, the monitoring service, the condition
    type, and the condition's metric name(s). No telemetry, no measured values, and
    no free-form customer payload is read — which is what keeps classification a
    metadata operation rather than a telemetry one.
    """
    ess = _essentials(raw)
    ctx = _alert_context(raw)
    parts: List[str] = [
        _value_of(ess.get("alertRule")),
        _value_of(ess.get("signalType")),
        _value_of(ess.get("monitoringService")),
        _value_of(ess.get("description")),
        _value_of(ctx.get("conditionType")),
    ]
    condition = ctx.get("condition") or {}
    all_of = condition.get("allOf") if isinstance(condition, dict) else None
    if isinstance(all_of, list):
        for criterion in all_of:
            if isinstance(criterion, dict):
                parts.append(_value_of(criterion.get("metricName")))
                parts.append(_value_of(criterion.get("metricNamespace")))
    return " ".join(p for p in parts if p).lower()


def classify_alert_signal(raw: Dict[str, Any]) -> Optional[str]:
    """Classify an App Insights alert into one of D3's signal kinds, or ``None``.

    Precedence is deliberate and tested: availability, then dependency failure,
    then application failure. An availability web-test alert usually also mentions
    "failure", and a dependency-failure alert is by definition also a failure, so
    without a fixed order the same alert could land in two kinds depending on token
    iteration. Most specific wins.

    Returns ``None`` when nothing is declared clearly enough to say. That is the
    honest outcome, not a gap to paper over: the record still carries its component
    reference, and D3 T2/T3 can act on the component without a kind. Forcing an
    unclassifiable alert into ``application_failure`` would make the kind field
    unfalsifiable.
    """
    text = _alert_declaration_text(raw)
    if not text:
        return None
    for token in _AVAILABILITY_TOKENS:
        if token in text:
            return SIGNAL_AVAILABILITY
    for token in _DEPENDENCY_FAILURE_TOKENS:
        if token in text:
            return SIGNAL_DEPENDENCY_FAILURE
    for token in _APPLICATION_FAILURE_TOKENS:
        if token in text:
            return SIGNAL_APPLICATION_FAILURE
    return None


def classify_health_signal(raw: Dict[str, Any]) -> Optional[str]:
    """Classify an App Insights health record as a health transition, or ``None``.

    A health record on the ``Microsoft.ResourceHealth/events`` surface that
    explicitly names an App Insights component is a health/failure event for the
    monitored application. The token check confirms the record is a health record
    (rather than something else that happens to name the component), keeping the
    classification explicit.
    """
    r = raw or {}
    props = r.get("properties") or {}
    text = " ".join(
        p for p in (
            _value_of(r.get("category")),
            _value_of(r.get("operationName")),
            _value_of(r.get("status")),
            _value_of(props.get("incidentType")),
            _value_of(props.get("stage")),
            _value_of(props.get("currentHealthStatus")),
            _value_of(props.get("previousHealthStatus")),
            _value_of(props.get("title")),
        ) if p
    ).lower()
    if not text:
        return None
    for token in _HEALTH_TRANSITION_TOKENS:
        if token in text:
            return SIGNAL_HEALTH_TRANSITION
    # An explicit health-status transition is a health transition even when the
    # record does not repeat a category token.
    if props.get("currentHealthStatus") or props.get("previousHealthStatus"):
        return SIGNAL_HEALTH_TRANSITION
    return None


# ── the exclusion gate (D3-AC2) ─────────────────────────────────────────────────


def _looks_like_analytics_result(raw: Dict[str, Any]) -> bool:
    """Whether the record is a Log Analytics / KQL query RESULT (tables+rows)."""
    tables = (raw or {}).get("tables")
    if not isinstance(tables, list) or not tables:
        return False
    first = tables[0]
    return isinstance(first, dict) and ("rows" in first or "columns" in first)


def _looks_like_metric_series(raw: Dict[str, Any]) -> bool:
    """Whether the record is an ARM/App Insights metric sample series."""
    r = raw or {}
    value = r.get("value")
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict) and ("timeseries" in first or "metricValues" in first):
            return True
    # App Insights REST metric shape: {"value": {"start": ..., "<metric>": {...}}}
    if isinstance(value, dict) and ("start" in value or "interval" in value):
        return True
    return bool(r.get("timeseries")) or bool(r.get("metricValues"))


def _declared_telemetry_type(raw: Dict[str, Any]) -> Optional[str]:
    """The excluded telemetry type this record declares, or ``None``.

    Reads the SDK/export envelope fields that name what a telemetry record is:
    ``itemType``, ``type``, ``baseType`` (inside ``data``), and the
    ``Microsoft.ApplicationInsights.<...>.<Type>`` envelope ``name``.
    """
    r = raw or {}
    candidates: List[str] = []
    for key in ("itemType", "type", "baseType", "telemetryType"):
        if isinstance(r.get(key), str):
            candidates.append(r[key])
    data = r.get("data")
    if isinstance(data, dict):
        for key in ("baseType", "itemType", "type"):
            if isinstance(data.get(key), str):
                candidates.append(data[key])
    name = r.get("name")
    if isinstance(name, str) and name.lower().startswith("microsoft.applicationinsights."):
        candidates.append(name.rsplit(".", 1)[-1])
    for candidate in candidates:
        token = candidate.strip().lower()
        if token in EXCLUDED_TELEMETRY_TYPES:
            return token
    return None


def is_excluded_telemetry(raw: Dict[str, Any]) -> bool:
    """Whether ``raw`` is raw telemetry / analytics output D3 must not ingest.

    Recognises Application Insights telemetry envelopes (requests, exceptions,
    dependency calls, page views, traces, metric samples, transactions, profiler
    output) and Log Analytics / KQL query results, so seeding any of them into a
    stream results in a loud drop rather than an ingested event (D3-AC2).

    **Conservative by construction.** An Azure Monitor alert or an Activity
    Log / health record short-circuits to ``False`` FIRST, before any telemetry
    marker is considered. Those envelopes are what MSP-B2 legitimately ingests
    today, and a scope guard that could swallow one of them would be a regression
    dressed as a safety feature. Everything else must present an unambiguous
    telemetry/analytics marker; an unrecognised record is NOT excluded, because
    this gate exists to keep known telemetry out, not to become an allow-list that
    silently narrows B2.
    """
    if not isinstance(raw, dict) or not raw:
        return False
    if _has_alert_envelope(raw) or _has_health_or_activity_envelope(raw):
        return False
    if _declared_telemetry_type(raw) is not None:
        return True
    if _looks_like_analytics_result(raw):
        return True
    if _looks_like_metric_series(raw):
        return True
    return False


# ── the scope result ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AppInsightsScope:
    """An in-scope App Insights operational signal, as transport metadata.

    ``signal_kind`` may be ``None``: the record explicitly references an App
    Insights component (so it IS in scope) but declares nothing that establishes
    which of D3's four kinds it is. Carried, not guessed.
    """

    component_id: str
    component_name: str
    surface: str
    signal_kind: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable form for the record wrapper / run health."""
        return {
            "component_id": self.component_id,
            "component_name": self.component_name,
            "surface": self.surface,
            "signal_kind": self.signal_kind,
        }


def app_insights_scope(raw: Dict[str, Any], *, surface: str) -> Optional[AppInsightsScope]:
    """Resolve a record's App Insights scope, or ``None`` when out of scope.

    The single entry point the connector uses. ``None`` means "this is not an App
    Insights operational signal" and leaves the record exactly as MSP-B2 already
    handles it — D3 never re-classifies, narrows, or drops a B2 record.

    Excluded telemetry returns ``None`` too, so this function can never be the
    route by which telemetry acquires an App Insights identity.
    """
    if surface not in (SURFACE_AZURE_MONITOR, SURFACE_AZURE_SERVICE_HEALTH):
        return None
    if not isinstance(raw, dict) or not raw:
        return None
    if is_excluded_telemetry(raw):
        return None

    component_id = referenced_component(raw, surface=surface)
    if component_id is None:
        return None

    if surface == SURFACE_AZURE_MONITOR:
        kind = classify_alert_signal(raw)
    else:
        kind = classify_health_signal(raw)

    return AppInsightsScope(
        component_id=component_id,
        component_name=component_name_from_resource_id(component_id) or "",
        surface=surface,
        signal_kind=kind,
    )
