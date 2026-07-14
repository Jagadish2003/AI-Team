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

# ─────────────────────────────────────────────────────────────────────────────
# Source-system ids (each resolves to a provider family in event_signature.py)
# ─────────────────────────────────────────────────────────────────────────────
SOURCE_AWS_CLOUDWATCH = "aws_cloudwatch"
SOURCE_AWS_EVENTBRIDGE = "aws"          # AWS's own event bus → the aws family
SOURCE_AWS_CLOUDTRAIL = "aws_cloudtrail"
SOURCE_AZURE_MONITOR = "azure_monitor"
SOURCE_AZURE_ACTIVITY = "azure_activity"


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


#: Registry of reference mappers by name — used by the golden-fixture harness and
#: available to connector implementers as the canonical mapper lookup.
MAPPERS = {
    "map_cloudwatch": map_cloudwatch,
    "map_eventbridge": map_eventbridge,
    "map_cloudtrail": map_cloudtrail,
    "map_azure_monitor": map_azure_monitor,
    "map_azure_activity_log": map_azure_activity_log,
}
