"""
app_insights_signal.py — 2.0-D3: the Application Insights signal vocabulary.

The PURE core of D3's Application Insights handling: which records are App
Insights operational signals, which of the four signal kinds each one is, and
which records are raw telemetry that must never be read. No I/O, no clients, no
``app`` import, no ARM knowledge — a deterministic function of a raw record.

Why it lives in ``signals/`` rather than ``ingest/``
----------------------------------------------------
Two consumers need the identical answers, and they sit on opposite sides of the
ingest boundary:

* ``discovery/ingest/azure_app_insights.py`` (D3 T1) — the bounded read scope, at
  the transport edge, deciding what the Azure connector may read and annotate.
* ``discovery/signals/reference_mappers.py`` (D3 T2) — the B0 mapper, which must
  derive the monitored application and the signal kind from the raw record on its
  own, because a mapper is invoked standalone (the golden-fixture harness calls
  ``MAPPERS[name](raw, org_id=...)`` with nothing else).

A second copy in the mapper would be the drift this repository has already been
bitten by once (three copies of the CI-dependency traversal, which had silently
diverged — see ``discovery/detectors/ci_dependency_graph.py``). And having the
mapper import from ``ingest`` would invert the layering: ``ingest`` modules import
``signals.reference_mappers``, so the reverse edge risks an import cycle.

So the rules live here, ONCE, and ``ingest/azure_app_insights.py`` re-exports
them — the same "one implementation, re-exported" shape ``security_ops_common``
uses for ``reachable_within``. A structural test asserts both modules resolve to
the SAME function objects, so a re-introduced private copy fails the build.

What stays in ``ingest/azure_app_insights.py``
---------------------------------------------
The genuinely transport-level parts: the permitted ARM path enumeration, the
excluded-endpoint markers, ``AppInsightsScopeViolation`` and
``assert_read_allowed`` — none of which a mapper has any business knowing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ── the Application Insights resource identity ──────────────────────────────────

#: The ARM resource type of an Application Insights component. An App Insights
#: "application" IS a resource of this type, which is what lets D3 ride the
#: existing Azure connector's rails rather than needing its own connection.
APP_INSIGHTS_RESOURCE_TYPE = "microsoft.insights/components"

#: The ARM id segment that EXPLICITLY marks an App Insights component. Matching is
#: on this full provider+type path (not merely "insights" or "components"), so an
#: unrelated ``microsoft.insights/*`` resource — a metric alert rule, an action
#: group, an autoscale setting, a web test — is never mistaken for a monitored
#: application.
COMPONENT_SEGMENT = "/providers/microsoft.insights/components/"


# ── the four operational signal kinds D3 names ──────────────────────────────────

#: An availability signal: the application is not answering (web/ping test).
SIGNAL_AVAILABILITY = "availability"
#: An application-failure signal: the application itself is erroring (failed
#: requests, server exceptions, Smart Detection failure anomalies).
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

#: The three kinds that describe a FAILURE (as opposed to a health transition).
#: Consulted by the B0 mapper's event-class rule: an ACTIVE one of these is an
#: ``error``, everything else is a ``state_change``.
FAILURE_SIGNAL_KINDS = frozenset({
    SIGNAL_AVAILABILITY,
    SIGNAL_APPLICATION_FAILURE,
    SIGNAL_DEPENDENCY_FAILURE,
})

#: The provider-native Application Insights event type for each signal kind — the
#: value the B0 mapper puts in ``event_type``. D3 requires that "the original
#: Application Insights event type must still be retained so users can distinguish
#: availability, dependency, failed-request, and health signals", and since
#: ``event_class`` necessarily collapses to the closed ``state_change``/``error``
#: vocabulary, ``event_type`` is where that distinction survives.
#:
#: It is also the token that participates in the deterministic signature, which is
#: why it is the KIND rather than the customer's alert-rule name: two different
#: rules both reporting "availability of app X is failing" are one recurring
#: operational fact, and folding them into one signature is what stops MSP-B7
#: counting the same problem twice because two rules noticed it. The rule name is
#: still carried, as bounded payload detail.
APP_INSIGHTS_EVENT_TYPES: Dict[str, str] = {
    SIGNAL_AVAILABILITY: "ApplicationInsights/Availability",
    SIGNAL_APPLICATION_FAILURE: "ApplicationInsights/ApplicationFailure",
    SIGNAL_DEPENDENCY_FAILURE: "ApplicationInsights/DependencyFailure",
    SIGNAL_HEALTH_TRANSITION: "ApplicationInsights/HealthTransition",
}

#: The event type for an in-scope record whose kind could not be established. A
#: distinct token rather than a guess, so an unclassifiable signal never
#: masquerades as one of the four.
APP_INSIGHTS_EVENT_TYPE_UNCLASSIFIED = "ApplicationInsights/Signal"


# ── the two surfaces D3 draws from (both already reached by MSP-B2) ──────────────

#: MSP-B0 source systems of the two surfaces. D3 adds no third surface.
SURFACE_AZURE_MONITOR = "azure_monitor"
SURFACE_AZURE_SERVICE_HEALTH = "azure_service_health"
APP_INSIGHTS_SURFACES = frozenset({SURFACE_AZURE_MONITOR, SURFACE_AZURE_SERVICE_HEALTH})


# ── the exclusion vocabulary (D3-AC2 scope defence) ─────────────────────────────

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


# ── record-shape helpers (read DECLARED fields only, never telemetry content) ────


def essentials(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The common-alert-schema ``data.essentials`` block, or {}."""
    return ((raw or {}).get("data") or {}).get("essentials") or {}


def alert_context(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The common-alert-schema ``data.alertContext`` block, or {}."""
    return ((raw or {}).get("data") or {}).get("alertContext") or {}


def value_of(field: Any) -> str:
    """Unwrap Azure's ``{"value": x}`` envelope, tolerating a plain scalar.

    The Activity Log / health surfaces wrap several fields this way while the alert
    surface does not, so every field read goes through here (the same tolerance
    ``azure_admin_events`` applies).
    """
    if isinstance(field, dict):
        return str(field.get("value") or "")
    return str(field or "")


def has_alert_envelope(raw: Dict[str, Any]) -> bool:
    """Whether the record is an Azure Monitor alert (has ``data.essentials``)."""
    return bool(essentials(raw))


def has_health_or_activity_envelope(raw: Dict[str, Any]) -> bool:
    """Whether the record is an Activity Log / health event record.

    Keyed on the fields those surfaces always carry. Used to short-circuit the
    telemetry check so a legitimate MSP-B2 record can never be mistaken for
    telemetry.
    """
    r = raw or {}
    return bool(r.get("eventDataId") or r.get("eventTimestamp") or r.get("operationName"))


def detect_surface(raw: Dict[str, Any]) -> Optional[str]:
    """Which of the two D3 surfaces a raw record came from, or ``None``.

    The record shapes are disjoint (an alert carries ``data.essentials``; a health
    record carries the Activity-Log-shaped envelope), so a standalone caller — the
    B0 mapper, invoked with nothing but the record — can resolve the surface
    without being told. Alert envelope wins if somehow both are present, because
    ``data.essentials`` is unambiguous.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    if has_alert_envelope(raw):
        return SURFACE_AZURE_MONITOR
    if has_health_or_activity_envelope(raw):
        return SURFACE_AZURE_SERVICE_HEALTH
    return None


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
    idx = rid.lower().find(COMPONENT_SEGMENT)
    if idx == -1:
        return None
    start = idx + len(COMPONENT_SEGMENT)
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


#: Documented fields in which Azure may supply an EXPLICIT reference to the
#: monitored application itself (rather than to its App Insights component). When
#: one is present it is preferred as the normalised resource, honouring D3's "the
#: explicit Application Insights component resource ID **or another explicit
#: monitored-application reference supplied by Azure**". Nothing is inferred: if
#: none of these carries a resource id, the component is used.
MONITORED_APPLICATION_FIELDS = (
    "monitoredApplicationResourceId",
    "applicationResourceId",
    "monitoredResourceId",
)


def monitored_application_reference(raw: Dict[str, Any], *, surface: str) -> Optional[str]:
    """An explicitly-supplied monitored-application resource id, or ``None``.

    Read ONLY from :data:`MONITORED_APPLICATION_FIELDS` on the alert context /
    health properties. This is the "or another explicit monitored-application
    reference" branch — it never guesses which of several targets is "the app".
    """
    blocks: List[Dict[str, Any]] = []
    if surface == SURFACE_AZURE_MONITOR:
        ctx = alert_context(raw)
        blocks.append(ctx)
        props = ctx.get("properties")
        if isinstance(props, dict):
            blocks.append(props)
    else:
        props = (raw or {}).get("properties")
        if isinstance(props, dict):
            blocks.append(props)
    for block in blocks:
        for key in MONITORED_APPLICATION_FIELDS:
            value = block.get(key)
            if value:
                return str(value)
    return None


def referenced_resource_ids(raw: Dict[str, Any], *, surface: str) -> List[str]:
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
        targets = essentials(raw).get("alertTargetIDs") or []
        if isinstance(targets, list):
            out.extend(str(t) for t in targets if t)
        alert_id = essentials(raw).get("alertId")
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
    for rid in referenced_resource_ids(raw, surface=surface):
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

#: ``monitorCondition`` values that mean the alert condition is NO LONGER active.
#: An alert in one of these states is a transition back to healthy, not an active
#: failure — the distinction the B0 mapper's event-class rule turns on.
RESOLVED_MONITOR_CONDITIONS = frozenset({"resolved", "deactivated", "cleared", "ok"})


def alert_declaration_text(raw: Dict[str, Any]) -> str:
    """The lower-cased concatenation of an alert's DECLARED descriptive fields.

    Exactly five sources, all part of the alert's own declaration of what it
    monitors: the rule name, the signal type, the monitoring service, the
    description, the condition type, and the condition's metric name(s). No
    telemetry, no measured values, and no free-form customer payload is read —
    which is what keeps classification a metadata operation rather than a
    telemetry one.
    """
    ess = essentials(raw)
    ctx = alert_context(raw)
    parts: List[str] = [
        value_of(ess.get("alertRule")),
        value_of(ess.get("signalType")),
        value_of(ess.get("monitoringService")),
        value_of(ess.get("description")),
        value_of(ctx.get("conditionType")),
    ]
    condition = ctx.get("condition") or {}
    all_of = condition.get("allOf") if isinstance(condition, dict) else None
    if isinstance(all_of, list):
        for criterion in all_of:
            if isinstance(criterion, dict):
                parts.append(value_of(criterion.get("metricName")))
                parts.append(value_of(criterion.get("metricNamespace")))
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
    reference. Forcing an unclassifiable alert into ``application_failure`` would
    make the kind field unfalsifiable.
    """
    text = alert_declaration_text(raw)
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

    A health record on the health surface that explicitly names an App Insights
    component is a health/failure event for the monitored application. The token
    check confirms the record is a health record (rather than something else that
    happens to name the component), keeping the classification explicit.
    """
    r = raw or {}
    props = r.get("properties") or {}
    text = " ".join(
        p for p in (
            value_of(r.get("category")),
            value_of(r.get("operationName")),
            value_of(r.get("status")),
            value_of(props.get("incidentType")),
            value_of(props.get("stage")),
            value_of(props.get("currentHealthStatus")),
            value_of(props.get("previousHealthStatus")),
            value_of(props.get("title")),
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


def is_alert_condition_active(raw: Dict[str, Any]) -> bool:
    """Whether an alert's condition is currently ACTIVE (not resolved).

    Azure states this explicitly in ``essentials.monitorCondition``. An absent
    value reads as ACTIVE: an alert record that does not say it has been resolved
    is the firing, and treating an unstated condition as resolved would silently
    downgrade a live failure to a routine transition.
    """
    condition = value_of(essentials(raw).get("monitorCondition")).strip().lower()
    if not condition:
        return True
    return condition not in RESOLVED_MONITOR_CONDITIONS


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
    silently narrows MSP-B2.
    """
    if not isinstance(raw, dict) or not raw:
        return False
    if has_alert_envelope(raw) or has_health_or_activity_envelope(raw):
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

    ``monitored_application_id`` is the resource the normalised event's
    ``resource`` will name — an explicitly-supplied monitored-application
    reference when Azure gave one, otherwise the App Insights component itself.
    Never the alert rule.
    """

    component_id: str
    component_name: str
    surface: str
    signal_kind: Optional[str] = None
    monitored_application_id: str = ""

    @property
    def application_id(self) -> str:
        """The monitored application's resource id (component when none supplied)."""
        return self.monitored_application_id or self.component_id

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable form for the record wrapper / run health."""
        return {
            "component_id": self.component_id,
            "component_name": self.component_name,
            "surface": self.surface,
            "signal_kind": self.signal_kind,
            "monitored_application_id": self.application_id,
        }


def app_insights_event_type(scope: AppInsightsScope, raw: Dict[str, Any]) -> str:
    """The provider-native App Insights event type the B0 mapper stamps.

    For a CLASSIFIED signal this is the kind's token, which is what makes two
    different rules reporting the same operational fact ("availability of app X is
    failing") fold to one recurrence signature.

    For an UNCLASSIFIED in-scope signal the customer's own alert-rule name is
    appended, because folding there would be an unjustified claim: we do not know
    the two conditions are the same operational fact, and the shared signature
    service guarantees that genuinely different events get different signatures.
    The rule name is the best available condition identity when the kind cannot be
    established, and it keeps a single slash-path grammar — which
    ``_normalize_event_type`` preserves for the azure family.
    """
    if scope.signal_kind:
        return APP_INSIGHTS_EVENT_TYPES[scope.signal_kind]
    rule = value_of(essentials(raw).get("alertRule")).strip()
    if rule:
        return f"{APP_INSIGHTS_EVENT_TYPE_UNCLASSIFIED}/{rule}"
    return APP_INSIGHTS_EVENT_TYPE_UNCLASSIFIED


def app_insights_scope(raw: Dict[str, Any], *, surface: str) -> Optional[AppInsightsScope]:
    """Resolve a record's App Insights scope, or ``None`` when out of scope.

    The single entry point both D3 consumers use. ``None`` means "this is not an
    App Insights operational signal" and leaves the record exactly as MSP-B2
    already handles it — D3 never re-classifies, narrows, or drops a B2 record.

    Excluded telemetry returns ``None`` too, so this function can never be the
    route by which telemetry acquires an App Insights identity.
    """
    if surface not in APP_INSIGHTS_SURFACES:
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
        monitored_application_id=(
            monitored_application_reference(raw, surface=surface) or ""
        ),
    )
