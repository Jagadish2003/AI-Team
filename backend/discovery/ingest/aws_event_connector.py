"""MSP-B1 / AT-641 — the AWS native event connector (B1's instantiation of T1).

MSP-B1 replaces the MSP-B8 export-and-load bridge for AWS with a live,
checkpointed feed: poll CloudWatch alarm history, EventBridge operational events,
and CloudTrail audit events across the managed accounts, and emit MSP-B0's
normalised operational events. Same schema, same mappers, same findings — the
transport goes native.

This module is deliberately thin. AT-641 built the shared
:class:`~discovery.ingest.cloud_event_connector.CloudEventConnector` skeleton;
the AWS connector IS that skeleton with ``provider='aws'`` and the three AWS
surfaces wired to their MSP-B0 mappers. MSP-B2's Azure connector is the SAME
skeleton with ``provider='azure'`` — if either needs to fork the poll loop, that
is a design defect (AT-641).

Offline vs live (AT-641 scope)
------------------------------
AT-641 (T1) delivers the skeleton and its OFFLINE proof: the poll source is
injectable, and :func:`build_offline_aws_source` reads a deterministic fixture so
a run works with no AWS account (the codebase's offline-first convention). The
LIVE poll source — a boto3-backed client polling CloudWatch/EventBridge/CloudTrail
with credentials resolved from the vault (per-run context, never process-global
env) — is a follow-up MSP-B1 subtask that depends on the SME-provided AWS
credentials; it drops in behind the same :class:`CloudPollSource` interface with
no change to this connector or the skeleton. No credential env var or placeholder
is introduced here (per the connector conventions, credentials live in the vault,
never in ``.env`` templates).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional

from .cloud_event_connector import (
    CloudEventConnector,
    CloudScope,
    StaticCloudPollSource,
)

PROVIDER_AWS = "aws"

#: AWS operational surfaces this connector polls (MSP-B1 scope).
SURFACE_CLOUDWATCH = "cloudwatch"
SURFACE_EVENTBRIDGE = "eventbridge"
SURFACE_CLOUDTRAIL = "cloudtrail"

#: Surface → MSP-B0 reference-mapper name (resolved via
#: ``discovery.signals.reference_mappers.MAPPERS`` inside the skeleton).
AWS_SURFACE_MAPPERS: Dict[str, str] = {
    SURFACE_CLOUDWATCH: "map_cloudwatch",
    SURFACE_EVENTBRIDGE: "map_eventbridge",
    SURFACE_CLOUDTRAIL: "map_cloudtrail",
}

#: All AWS surfaces, in a stable order.
AWS_SURFACES = (SURFACE_CLOUDWATCH, SURFACE_EVENTBRIDGE, SURFACE_CLOUDTRAIL)

_OFFLINE_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "aws_native_events_sample.json"
)


class AWSEventConnector(CloudEventConnector):
    """Native AWS event connector — the shared skeleton bound to ``provider='aws'``.

    Adds nothing to the skeleton but the AWS identity; construct it with a
    :class:`~discovery.ingest.cloud_event_connector.CloudPollSource` (offline via
    :func:`build_offline_aws_source`, live in a follow-up subtask).
    """

    provider = PROVIDER_AWS
    connector_id = "aws_events"


def aws_scope(
    account: str,
    surface: str,
    *,
    region: Optional[str] = None,
    label: Optional[str] = None,
) -> CloudScope:
    """Build a :class:`CloudScope` for one AWS ``(account, surface[, region])``.

    Raises ``ValueError`` for a surface outside :data:`AWS_SURFACE_MAPPERS` so a
    typo surfaces at construction rather than as an unroutable scope at run time.
    """
    if surface not in AWS_SURFACE_MAPPERS:
        raise ValueError(
            f"unknown AWS surface {surface!r}; expected one of {sorted(AWS_SURFACE_MAPPERS)}"
        )
    return CloudScope(
        provider=PROVIDER_AWS,
        account=account,
        surface=surface,
        mapper=AWS_SURFACE_MAPPERS[surface],
        region=region,
        label=label,
    )


def aws_scopes(
    accounts: Iterable[str],
    *,
    regions: Optional[Iterable[Optional[str]]] = None,
    surfaces: Iterable[str] = AWS_SURFACES,
) -> List[CloudScope]:
    """Build the CloudWatch/EventBridge/CloudTrail scopes for the managed accounts.

    One scope per ``(account, region, surface)``. ``regions`` defaults to a single
    unspecified region (``None``); pass explicit regions for a regional deployment.
    """
    region_list = list(regions) if regions is not None else [None]
    scopes: List[CloudScope] = []
    for account in accounts:
        for region in region_list:
            for surface in surfaces:
                scopes.append(aws_scope(account, surface, region=region))
    return scopes


def build_offline_aws_source(
    fixture_path: Optional[str] = None,
    *,
    page_size: int = 500,
) -> StaticCloudPollSource:
    """Build an offline AWS poll source from the deterministic fixture.

    The fixture is a ``{"scopes": [{"account","surface","region"?,"events":[...]}]}``
    document; each entry's raw ``events`` are the provider payloads a live poll
    would return for that surface. Used by default in offline mode so a run works
    with no AWS account. A missing fixture yields an empty source (no scopes).
    """
    path = fixture_path or _OFFLINE_FIXTURE
    if not os.path.exists(path):
        return StaticCloudPollSource([], page_size=page_size)
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    scope_events: List[Any] = []
    for entry in doc.get("scopes", []):
        scope = aws_scope(
            entry["account"],
            entry["surface"],
            region=entry.get("region"),
            label=entry.get("label"),
        )
        scope_events.append((scope, entry.get("events", [])))
    return StaticCloudPollSource(scope_events, page_size=page_size)
