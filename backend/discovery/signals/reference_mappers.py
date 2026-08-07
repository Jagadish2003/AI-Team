"""MSP-B0 / AT-637 — provider mapping contract + reference mappers.

Each cloud source emits a provider-specific payload; a **mapper** is the function
that translates one raw provider payload into the normalised
:class:`~discovery.signals.operational_event.OperationalEvent` (MSP-B0). These are
the *reference* mappers — they define, by executable example, exactly how a
connector implementer maps their provider onto the common schema. They operate on
**golden fixtures**, not live connections (live ingestion is B1/B2/B8); the
fixtures pin the mapping behaviour so a future live connector can be checked
against the same expectations.

Two provider families, five representative surfaces
---------------------------------------------------
* **AWS** — :func:`map_cloudwatch` (CloudWatch alarm state change),
  :func:`map_eventbridge` (generic EventBridge event, e.g. an EC2 state change),
  :func:`map_cloudtrail` (management/API activity record).
* **Azure** — :func:`map_azure_monitor` (common-alert-schema alert),
  :func:`map_azure_activity_log` (Activity Log administrative record).

The mapping contract (what every mapper must produce)
-----------------------------------------------------
Whatever the provider shape, the mapper resolves the same target fields and hands
them to :meth:`OperationalEvent.build`, which normalises the vocabularies, mints
the OBSERVED provenance pointer, and derives the recurrence ``event_signature``:

======================  ====================================================
schema field            resolved from
======================  ====================================================
``source_system``       fixed per surface (``aws_cloudwatch`` / ``aws`` /
                        ``aws_cloudtrail`` / ``azure_monitor`` /
                        ``azure_activity``) — resolves to the provider family.
``signal_id``           the provider's stable per-event id.
``event_type``          the provider-native event name (preserved verbatim).
``event_class``         classified from the provider's operation/state.
``resource_type``       derived from the resource id (ARN / Azure resource id).
``severity``            mapped from the provider's severity/state/level.
``observed_at``         the provider's event timestamp.
``resource``            a :class:`ResourceRef` over the affected resource.
``message``             a short human-readable summary.
``payload``             a curated subset of provider detail (incl. ``principal``
                        for access/audit events, so the signature keys on it).
======================  ====================================================

Because every mapper terminates in ``OperationalEvent.build``, all providers emit
the **identical detector-visible structure** (T3-AC3) — a detector never branches
on provider. Mappers are tolerant (missing optional fields degrade to sensible
defaults) and never crash a run, consistent with the connector conventions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .operational_event import OperationalEvent, ResourceRef, normalize_resource_type

# 2.0-D3 T2 — the shared Application Insights signal vocabulary. The SAME rules
# the ingest-side bounded read scope uses (``ingest/azure_app_insights.py``
# re-exports them), so the mapper and the read path can never disagree about what
# an App Insights signal is. Aliased on import so the App Insights helpers are
# visibly distinct from this module's own generic ones.
from .app_insights_signal import (
    FAILURE_SIGNAL_KINDS,
    SIGNAL_HEALTH_TRANSITION,
    SURFACE_AZURE_MONITOR,
    alert_context as ai_alert_context,
    app_insights_event_type as ai_event_type,
    app_insights_scope as ai_scope,
    detect_surface as ai_detect_surface,
    essentials as ai_essentials,
    is_alert_condition_active,
    value_of as ai_value_of,
)

# ─────────────────────────────────────────────────────────────────────────────
# Source-system ids (each resolves to a provider family in event_signature.py)
# ─────────────────────────────────────────────────────────────────────────────
SOURCE_AWS_CLOUDWATCH = "aws_cloudwatch"
SOURCE_AWS_EVENTBRIDGE = "aws"          # AWS's own event bus → the aws family
SOURCE_AWS_CLOUDTRAIL = "aws_cloudtrail"
SOURCE_AZURE_MONITOR = "azure_monitor"
SOURCE_AZURE_ACTIVITY = "azure_activity"
SOURCE_AZURE_SERVICE_HEALTH = "azure_service_health"
#: 2.0-D3 T2 — Application Insights operational signals. A distinct source system
#: (so a report can say where the signal came from) that resolves to the SAME
#: ``azure`` provider family as every other Azure surface, which is what keeps its
#: signature comparable with them. Registered in
#: ``event_signature._SOURCE_SYSTEM_FAMILY``.
SOURCE_AZURE_APP_INSIGHTS = "azure_app_insights"


# ─────────────────────────────────────────────────────────────────────────────
# Resource-type derivation (provider-native id → normalised resource_type)
# ─────────────────────────────────────────────────────────────────────────────
_AWS_SERVICE_RESOURCE_TYPE: Dict[str, str] = {
    "ec2": "compute", "lambda": "serverless",
    "ecs": "container", "eks": "container",
    "s3": "storage", "ebs": "storage", "efs": "storage",
    "rds": "database", "dynamodb": "database", "elasticache": "database",
    "ec2vpc": "network", "elasticloadbalancing": "network", "route53": "network",
    "iam": "identity", "kms": "identity", "sts": "identity",
    "sns": "messaging", "sqs": "messaging", "events": "messaging",
    "cloudwatch": "monitoring", "logs": "monitoring",
    "guardduty": "security", "securityhub": "security",
}

_AZURE_TYPE_RESOURCE_TYPE: Dict[str, str] = {
    "virtualmachines": "compute", "sites": "compute", "serverfarms": "compute",
    "containergroups": "container", "managedclusters": "container",
    "storageaccounts": "storage", "disks": "storage",
    "servers": "database", "databases": "database", "redis": "database",
    "virtualnetworks": "network", "loadbalancers": "network",
    "publicipaddresses": "network", "dnszones": "network",
    "vaults": "identity", "userassignedidentities": "identity",
    "namespaces": "messaging", "eventhubs": "messaging", "queues": "messaging",
    "components": "monitoring", "metricalerts": "monitoring",
    "activitylogalerts": "monitoring",
}


def _aws_service_from_arn(arn: Optional[str]) -> str:
    """Extract the service token from an ARN (``arn:partition:service:...``)."""
    parts = str(arn or "").split(":")
    return parts[2].strip().lower() if len(parts) > 2 else ""


def aws_resource_type_from_arn(arn: Optional[str]) -> str:
    """Map an AWS ARN's service to a normalised ``resource_type`` (never raises)."""
    svc = _aws_service_from_arn(arn)
    if svc in _AWS_SERVICE_RESOURCE_TYPE:
        return _AWS_SERVICE_RESOURCE_TYPE[svc]
    return normalize_resource_type(svc)


def _aws_resource_type_from_event_source(event_source: Optional[str]) -> str:
    """Map a CloudTrail ``eventSource`` (``ec2.amazonaws.com``) to a resource_type."""
    svc = str(event_source or "").split(".")[0].strip().lower()
    if svc in _AWS_SERVICE_RESOURCE_TYPE:
        return _AWS_SERVICE_RESOURCE_TYPE[svc]
    return normalize_resource_type(svc)


def _azure_provider_type(resource_id: Optional[str]) -> str:
    """Extract the Azure resource type (``virtualMachines``) from a resource id."""
    rid = str(resource_id or "")
    low = rid.lower()
    idx = low.rfind("/providers/")
    seg = rid[idx + len("/providers/"):] if idx >= 0 else rid
    parts = [p for p in seg.split("/") if p]
    # parts ~ ["Microsoft.Compute", "virtualMachines", "vm1", ...]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else ""


def azure_resource_type_from_id(resource_id: Optional[str]) -> str:
    """Map an Azure resource id's provider type to a normalised resource_type."""
    t = _azure_provider_type(resource_id).strip().lower()
    if t in _AZURE_TYPE_RESOURCE_TYPE:
        return _AZURE_TYPE_RESOURCE_TYPE[t]
    return normalize_resource_type(t)


# ─────────────────────────────────────────────────────────────────────────────
# AWS mappers
# ─────────────────────────────────────────────────────────────────────────────

#: CloudWatch alarm state → severity. An alarm entering ALARM is a high-severity
#: signal; OK is informational; INSUFFICIENT_DATA sits between.
_CLOUDWATCH_STATE_SEVERITY = {
    "ALARM": "high",
    "OK": "info",
    "INSUFFICIENT_DATA": "medium",
}


def map_cloudwatch(payload: Dict[str, Any], *, org_id: str) -> OperationalEvent:
    """Map a CloudWatch Alarm State Change event to an OperationalEvent.

    The alarm state transition is a monitor ``state_change``; severity follows the
    new alarm state. The affected resource is the alarm itself
    (``resource_type='monitoring'``).
    """
    detail = payload.get("detail") or {}
    state = (detail.get("state") or {}).get("value", "")
    previous = (detail.get("previousState") or {}).get("value", "")
    resources: List[str] = payload.get("resources") or []
    alarm_arn = resources[0] if resources else ""
    alarm_name = detail.get("alarmName") or ""
    region = payload.get("region")

    resource = None
    if alarm_arn:
        resource = ResourceRef(
            provider="aws",
            resource_type=aws_resource_type_from_arn(alarm_arn),
            resource_id=alarm_arn,
            region=region,
            name=alarm_name or None,
        )

    return OperationalEvent.build(
        org_id=org_id,
        source_system=SOURCE_AWS_CLOUDWATCH,
        signal_id=payload.get("id") or f"cloudwatch:{alarm_name}",
        observed_at=payload.get("time"),
        event_type=payload.get("detail-type") or "CloudWatch Alarm State Change",
        event_class="state_change",
        severity=_CLOUDWATCH_STATE_SEVERITY.get(str(state).upper(), "info"),
        resource=resource,
        message=(detail.get("state") or {}).get("reason") or alarm_name or None,
        payload={
            "alarm_name": alarm_name,
            "state": state,
            "previous_state": previous,
            "account": payload.get("account"),
            "region": region,
        },
    )


def _classify_eventbridge(detail_type: str) -> str:
    """Classify a generic EventBridge event by its detail-type."""
    dt = str(detail_type or "").lower()
    if "state-change" in dt or "state change" in dt:
        return "state_change"
    if any(k in dt for k in ("created", "launch", "terminat", "deleted", "started", "stopped")):
        return "lifecycle"
    if "error" in dt or "fail" in dt:
        return "error"
    return "other"


def map_eventbridge(payload: Dict[str, Any], *, org_id: str) -> OperationalEvent:
    """Map a generic AWS EventBridge event (e.g. EC2 state change) to an OperationalEvent.

    Uses the EventBridge envelope's ``detail-type`` as the event name and
    ``resources[0]`` as the affected resource. EventBridge events carry no
    severity, so severity defaults to ``info``.
    """
    detail = payload.get("detail") or {}
    resources: List[str] = payload.get("resources") or []
    resource_arn = resources[0] if resources else ""
    region = payload.get("region")
    detail_type = payload.get("detail-type") or ""

    resource = None
    if resource_arn:
        resource = ResourceRef(
            provider="aws",
            resource_type=aws_resource_type_from_arn(resource_arn),
            resource_id=resource_arn,
            region=region,
        )

    return OperationalEvent.build(
        org_id=org_id,
        source_system=SOURCE_AWS_EVENTBRIDGE,
        signal_id=payload.get("id") or f"eventbridge:{detail_type}",
        observed_at=payload.get("time"),
        event_type=detail_type,
        event_class=_classify_eventbridge(detail_type),
        severity="info",
        resource=resource,
        message=detail_type or None,
        payload={
            # Curated, normalised scalars only — the raw provider `detail` blob is
            # NOT embedded here; it lives in the evidence store and is reached via
            # the event's evidence pointer (AT-638 / T4-AC4).
            "source": payload.get("source"),
            "account": payload.get("account"),
            "region": region,
            "state": detail.get("state"),
        },
    )


def map_cloudtrail(payload: Dict[str, Any], *, org_id: str) -> OperationalEvent:
    """Map a CloudTrail management/API record to an OperationalEvent.

    A CloudTrail record is an API call, so the event class is ``access`` — and a
    failed call (``errorCode`` present) is an ``error``. The calling principal
    (``userIdentity.arn``) is carried on the payload so the signature keys on the
    actor for these access/error events.
    """
    error_code = payload.get("errorCode")
    is_error = bool(error_code)
    user_identity = payload.get("userIdentity") or {}
    principal = user_identity.get("arn") or user_identity.get("principalId") or ""
    event_source = payload.get("eventSource") or ""

    resources = payload.get("resources") or []
    resource = None
    if resources and isinstance(resources[0], dict):
        arn = resources[0].get("ARN") or ""
        if arn:
            resource = ResourceRef(
                provider="aws",
                resource_type=aws_resource_type_from_arn(arn),
                resource_id=arn,
                region=payload.get("awsRegion"),
            )
    if resource is None and event_source:
        # No explicit resource ARN — reference the service the call targeted.
        resource = ResourceRef(
            provider="aws",
            resource_type=_aws_resource_type_from_event_source(event_source),
            resource_id=event_source,
            region=payload.get("awsRegion"),
        )

    return OperationalEvent.build(
        org_id=org_id,
        source_system=SOURCE_AWS_CLOUDTRAIL,
        signal_id=payload.get("eventID") or f"cloudtrail:{payload.get('eventName', '')}",
        observed_at=payload.get("eventTime"),
        event_type=payload.get("eventName") or "",
        event_class="error" if is_error else "access",
        severity="high" if is_error else "info",
        resource=resource,
        message=payload.get("errorMessage") or payload.get("eventName") or None,
        payload={
            "principal": principal,
            "event_source": event_source,
            "error_code": error_code,
            "read_only": payload.get("readOnly"),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Azure mappers
# ─────────────────────────────────────────────────────────────────────────────

def map_azure_monitor(payload: Dict[str, Any], *, org_id: str) -> OperationalEvent:
    """Map an Azure Monitor common-alert-schema alert to an OperationalEvent.

    Mirrors CloudWatch: an alert monitor-condition transition is a ``state_change``.
    Severity is the Azure ``Sev0..Sev4`` ladder normalised onto the schema
    vocabulary; the resource is the first ``alertTargetIDs`` entry.
    """
    data = payload.get("data") or {}
    essentials = data.get("essentials") or {}
    targets: List[str] = essentials.get("alertTargetIDs") or []
    target_id = targets[0] if targets else ""

    resource = None
    if target_id:
        resource = ResourceRef(
            provider="azure",
            resource_type=azure_resource_type_from_id(target_id),
            resource_id=target_id,
        )

    event_type = (
        essentials.get("alertRule")
        or essentials.get("signalType")
        or "AzureMonitorAlert"
    )

    return OperationalEvent.build(
        org_id=org_id,
        source_system=SOURCE_AZURE_MONITOR,
        signal_id=essentials.get("alertId") or f"azure_monitor:{event_type}",
        observed_at=essentials.get("firedDateTime"),
        event_type=event_type,
        event_class="state_change",
        severity=essentials.get("severity"),   # normalised by build() (Sev2 → high)
        resource=resource,
        message=essentials.get("description") or None,
        payload={
            "monitor_condition": essentials.get("monitorCondition"),
            "signal_type": essentials.get("signalType"),
            "severity_raw": essentials.get("severity"),
        },
    )


def _classify_activity_log(operation_name: str, status: str) -> str:
    """Classify an Activity Log operation by its verb (and failure status)."""
    if str(status or "").strip().lower() in ("failed", "failure", "error"):
        return "error"
    verb = str(operation_name or "").rstrip("/").rsplit("/", 1)[-1].strip().lower()
    if verb == "write":
        return "configuration"
    if verb == "delete":
        return "lifecycle"
    if verb in ("action", "read", "listkeys"):
        return "access"
    return "configuration"


def map_azure_activity_log(payload: Dict[str, Any], *, org_id: str) -> OperationalEvent:
    """Map an Azure Activity Log administrative record to an OperationalEvent.

    The ``operationName`` (``Microsoft.Compute/virtualMachines/write``) is the
    event name and drives the event-class classification; the ``caller`` is the
    acting principal, carried on the payload for the signature.
    """
    operation = payload.get("operationName") or ""
    status = (payload.get("status") or {})
    status_val = status.get("value") if isinstance(status, dict) else status
    resource_id = payload.get("resourceId") or ""
    caller = payload.get("caller") or ""
    category = payload.get("category")
    category_val = category.get("value") if isinstance(category, dict) else category

    resource = None
    if resource_id:
        resource = ResourceRef(
            provider="azure",
            resource_type=azure_resource_type_from_id(resource_id),
            resource_id=resource_id,
        )

    return OperationalEvent.build(
        org_id=org_id,
        source_system=SOURCE_AZURE_ACTIVITY,
        signal_id=payload.get("eventDataId") or payload.get("correlationId") or f"azure_activity:{operation}",
        observed_at=payload.get("eventTimestamp"),
        event_type=operation,
        event_class=_classify_activity_log(operation, status_val),
        severity=payload.get("level"),          # normalised by build() (Informational → info)
        resource=resource,
        message=f"{operation} {status_val}".strip() or None,
        payload={
            "principal": caller,
            "category": category_val,
            "status": status_val,
            "correlation_id": payload.get("correlationId"),
        },
    )


def map_service_health(payload: Dict[str, Any], *, org_id: str) -> OperationalEvent:
    """Map an Azure Service Health event to an OperationalEvent (MSP-B2 T3).

    Service Health events surface Azure-side service issues, planned maintenance,
    and health/security advisories for a service in a region. They arrive as
    Activity-Log-shaped records (``category='ServiceHealth'``) whose
    service-health specifics live under ``properties``. Mirrors the other Azure
    mappers: the event is a service-status ``state_change`` (an active service
    issue is an ``error``), the ``incidentType`` is the provider-native event type,
    and the affected Azure *service* is the resource the event concerns (Service
    Health is service/region-scoped, not a single resource id).
    """
    props = payload.get("properties")
    if not isinstance(props, dict):
        props = {}

    incident_type = str(props.get("incidentType") or props.get("incident_type") or "")
    stage = str(props.get("stage") or "")
    status = payload.get("status")
    status_val = status.get("value") if isinstance(status, dict) else status
    service = str(props.get("service") or "")
    region = str(props.get("region") or props.get("impactedRegion") or "")
    title = str(props.get("title") or "")

    # An active/ongoing service issue is an error; anything resolved / advisory /
    # maintenance is a state_change (a status transition), classified via the
    # shared vocabulary by build().
    active = str(stage or status_val or "").strip().lower() in ("active", "ongoing", "in-progress", "inprogress")
    is_issue = incident_type.strip().lower() in ("incident", "serviceissue", "service_issue")
    event_class = "error" if (active and is_issue) else "state_change"

    resource = None
    if service:
        resource = ResourceRef(
            provider="azure",
            resource_type=normalize_resource_type(service),
            resource_id=service,
        )

    return OperationalEvent.build(
        org_id=org_id,
        source_system=SOURCE_AZURE_SERVICE_HEALTH,
        signal_id=(
            props.get("trackingId")
            or payload.get("eventDataId")
            or payload.get("correlationId")
            or f"azure_service_health:{incident_type or 'event'}"
        ),
        observed_at=payload.get("eventTimestamp") or props.get("impactStartTime"),
        event_type=incident_type or "ServiceHealthEvent",
        event_class=event_class,
        severity=payload.get("level"),   # Informational/Warning/... → normalised
        resource=resource,
        message=title or (f"{incident_type} {stage}".strip() or None),
        payload={
            "incident_type": incident_type,
            "service": service,
            "region": region,
            "stage": stage,
            "status": status_val,
            "tracking_id": props.get("trackingId"),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Application Insights mapper (2.0-D3 T2)
# ─────────────────────────────────────────────────────────────────────────────


def _app_insights_event_class(scope, raw: Dict[str, Any]) -> str:
    """The B0 event class for an App Insights signal.

    The story talks about "alarm/health" events, but B0's ``EVENT_CLASSES`` is a
    CLOSED vocabulary with no such tokens, so D3's rule maps onto it as follows:

      * a **health transition** is a ``state_change`` — that is precisely what a
        health-state move is;
      * an **ACTIVE** availability / application / dependency failure is an
        ``error`` — the application or something it depends on is failing right
        now;
      * anything else — a RESOLVED failure alert, or an in-scope alert whose kind
        could not be established — is a ``state_change``, because what the record
        reports is a transition rather than a live fault.

    The distinction between the last two branches is read from Azure's own
    ``monitorCondition``, never inferred (see ``is_alert_condition_active``).

    Consequence worth stating plainly: a failure alert firing and the same alert
    resolving carry DIFFERENT classes and therefore different signatures. That is
    intended — they are different operational facts — and it is what lets
    recurrence detection count firings without a resolution diluting the count.
    Repeated FIRINGS of the same condition on the same application still fold to
    one signature, which is the property D3 requires.
    """
    kind = getattr(scope, "signal_kind", None)
    if kind == SIGNAL_HEALTH_TRANSITION:
        return "state_change"
    if kind in FAILURE_SIGNAL_KINDS and is_alert_condition_active(raw):
        return "error"
    return "state_change"


def _app_insights_payload(scope, raw: Dict[str, Any]) -> Dict[str, Any]:
    """The BOUNDED, curated payload for an App Insights event.

    Curated, never the raw record: the complete Azure payload lives only in the
    MSP-B0 raw-event evidence store (AT-638), reachable through the event's
    OBSERVED pointer. What is kept here is the provider-native detail that explains
    the signal — the alert rule that fired, the condition type, the metric, the
    monitoring service, the health-status transition — plus the App Insights signal
    kind as a machine token.

    Deliberately excludes any key the signature's principal lookup consults
    (``principal``/``actor``/``user``/``caller``): these are ``state_change`` and
    ``error`` events, whose recipe has no principal, and introducing one of those
    keys would silently change what the fingerprint keys on.

    Empty values are dropped, so the payload never carries a field the provider did
    not state.
    """
    ess = ai_essentials(raw)
    ctx = ai_alert_context(raw)
    props = (raw or {}).get("properties") or {}

    metric_names: List[str] = []
    condition = ctx.get("condition") or {}
    all_of = condition.get("allOf") if isinstance(condition, dict) else None
    if isinstance(all_of, list):
        for criterion in all_of:
            if isinstance(criterion, dict) and criterion.get("metricName"):
                metric_names.append(str(criterion["metricName"]))

    candidate: Dict[str, Any] = {
        # The App Insights identity of the signal — the field that keeps
        # availability / application-failure / dependency-failure / health
        # distinguishable after event_class has collapsed to the B0 vocabulary.
        "app_insights_signal_kind": scope.signal_kind,
        "app_insights_component_id": scope.component_id,
        "app_insights_component_name": scope.component_name,
        # Alert-surface detail.
        "alert_rule": ai_value_of(ess.get("alertRule")) or None,
        "monitor_condition": ai_value_of(ess.get("monitorCondition")) or None,
        "signal_type": ai_value_of(ess.get("signalType")) or None,
        "monitoring_service": ai_value_of(ess.get("monitoringService")) or None,
        "condition_type": ai_value_of(ctx.get("conditionType")) or None,
        "metric_name": metric_names[0] if metric_names else None,
        "severity_raw": ai_value_of(ess.get("severity")) or None,
        # Health-surface detail.
        "current_health_status": ai_value_of(props.get("currentHealthStatus")) or None,
        "previous_health_status": ai_value_of(props.get("previousHealthStatus")) or None,
        "health_status": ai_value_of(raw.get("status")) or None,
        "incident_type": ai_value_of(props.get("incidentType")) or None,
    }
    return {k: v for k, v in candidate.items() if v not in (None, "")}


def map_app_insights(payload: Dict[str, Any], *, org_id: str) -> OperationalEvent:
    """Map an Application Insights operational signal to an OperationalEvent (2.0-D3 T2).

    Accepts a record from EITHER surface D3 draws on — an Azure Monitor alert or an
    Azure health event — and resolves the surface from the record's own shape, so
    the mapper is invocable standalone exactly like every other reference mapper.

    The four things D3 requires of this mapping:

    * **Resource = the monitored application, never the alert rule.** The resource
      id is the explicitly-supplied monitored-application reference when Azure gave
      one, otherwise the Application Insights component itself — both explicit
      references, never inferred. ``resource_type`` is derived by the SAME shared
      ``azure_resource_type_from_id`` every other Azure mapper uses (a
      ``microsoft.insights/components`` id resolves to ``monitoring``); D3 does not
      override that table, because inventing a per-surface resource-type rule would
      be a provider-specific carve-out in shared code, and nothing downstream
      branches on ``resource_type``.
    * **Event class** per :func:`_app_insights_event_class`.
    * **The original App Insights event type is retained** — ``event_type`` is the
      provider-native ``ApplicationInsights/<Signal>`` token, so availability,
      application-failure, dependency-failure and health signals stay
      distinguishable after the class has collapsed to B0's closed vocabulary. The
      alert rule, condition type and metric name are carried as bounded payload.
    * **Signature via the existing deterministic service.** ``build`` derives it
      through ``compute_event_signature``; nothing is computed here. Because that
      service keys on ``(family, class, resource_type, event_type, resource_id)``,
      repeated occurrences of the same condition on the same application fold to
      one signature, while timestamp, severity, free-form description and the
      per-occurrence alert id are structurally excluded from it.

    Raises ``ValueError`` when the record is not an App Insights operational signal
    (out of scope, or excluded telemetry). The connector's stream engine treats a
    mapper failure as a loud per-record skip, so an out-of-scope record can never
    be silently normalised into something it is not.
    """
    surface = ai_detect_surface(payload)
    if surface is None:
        raise ValueError(
            "map_app_insights: record matches neither the Azure Monitor alert nor "
            "the Azure health event shape"
        )
    scope = ai_scope(payload, surface=surface)
    if scope is None:
        raise ValueError(
            "map_app_insights: record is not an Application Insights operational "
            "signal (no explicit component reference, or excluded telemetry)"
        )

    ess = ai_essentials(payload)
    props = (payload or {}).get("properties") or {}

    # The monitored application: an explicitly-supplied reference when Azure gave
    # one, otherwise the App Insights component. Never the alert rule.
    application_id = scope.application_id
    resource = ResourceRef(
        provider="azure",
        resource_type=azure_resource_type_from_id(application_id),
        resource_id=application_id,
    )
    event_type = ai_event_type(scope, payload)

    if surface == SURFACE_AZURE_MONITOR:
        signal_id = str(ess.get("alertId") or "") or f"azure_app_insights:{event_type}"
        observed_at = ai_value_of(ess.get("firedDateTime")) or None
        severity = ess.get("severity")
        message = ai_value_of(ess.get("description")) or None
    else:
        signal_id = str(
            props.get("trackingId")
            or payload.get("eventDataId")
            or payload.get("correlationId")
            or f"azure_app_insights:{event_type}"
        )
        observed_at = (
            ai_value_of(payload.get("eventTimestamp"))
            or ai_value_of(props.get("impactStartTime"))
            or None
        )
        severity = payload.get("level")
        message = ai_value_of(props.get("title")) or None

    return OperationalEvent.build(
        org_id=org_id,
        source_system=SOURCE_AZURE_APP_INSIGHTS,
        signal_id=signal_id,
        observed_at=observed_at,
        event_type=event_type,
        event_class=_app_insights_event_class(scope, payload),
        severity=severity,          # Sev0..4 / Warning / Error → normalised by build()
        resource=resource,
        message=message,
        payload=_app_insights_payload(scope, payload),
    )


#: Registry of reference mappers by name — used by the golden-fixture harness and
#: available to connector implementers as the canonical mapper lookup.
MAPPERS = {
    "map_cloudwatch": map_cloudwatch,
    "map_eventbridge": map_eventbridge,
    "map_cloudtrail": map_cloudtrail,
    "map_azure_monitor": map_azure_monitor,
    "map_azure_activity_log": map_azure_activity_log,
    "map_service_health": map_service_health,
    "map_app_insights": map_app_insights,
}
