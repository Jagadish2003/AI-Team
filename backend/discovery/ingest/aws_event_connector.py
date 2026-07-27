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

Offline vs live
---------------
The poll source is injectable. :func:`build_offline_aws_source` reads a
deterministic fixture so a run works with no AWS account (the codebase's
offline-first convention); :mod:`discovery.ingest.aws_poll_source` supplies the
LIVE boto3-backed source (AT-642) with credentials resolved from the per-org
vault. :func:`build_ingestor` is the single entry point the discovery runner
calls: it picks the right source for the mode and resolves the org's pinned
managed-account estate through :mod:`discovery.ingest.aws_events_config`. No
credential env var or placeholder is introduced here (per the connector
conventions, credentials live in the vault, never in ``.env`` templates).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .cloud_event_connector import (
    CloudEventConnector,
    CloudPollSource,
    CloudScope,
    StaticCloudPollSource,
)

logger = logging.getLogger(__name__)

PROVIDER_AWS = "aws"

#: AWS operational surfaces this connector knows about (MSP-B1 scope).
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

#: Every AWS surface vocabulary entry, in a stable order. This is the set of
#: surfaces the connector CAN map — not the set it polls live by default (see
#: :data:`DEFAULT_POLL_SURFACES`).
AWS_SURFACES = (SURFACE_CLOUDWATCH, SURFACE_EVENTBRIDGE, SURFACE_CLOUDTRAIL)

#: The surfaces the LIVE poll source reads by default — ALL THREE.
#:
#: This deliberately matches :data:`AWS_SURFACES`. MSP-B1's SCOPE DEFENCE names
#: three V1 event classes and AC1 requires all three to be ingested from every
#: managed account, so the bounded EventBridge surface is IN scope by default and
#: must not be quietly dropped. What that surface can honestly observe under the
#: story's minimal grant (``events:Describe*/List*``) is the bounded RULE SET —
#: the bus exposes no past-event read API — so the connector reports rule-state
#: observations and rule CHANGES, which is what "archive/replay-adjacent reads on
#: the bounded rule set" (Section 1) describes. The boundary is documented at
#: :data:`discovery.ingest.aws_poll_source.EVENTBRIDGE_SURFACE_NOTE` rather than
#: hidden; widening to a true event stream is a new story, not a default change.
DEFAULT_POLL_SURFACES: Tuple[str, ...] = AWS_SURFACES

_OFFLINE_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "aws_native_events_sample.json"
)


class AWSEventConnector(CloudEventConnector):
    """Native AWS event connector — the shared skeleton bound to ``provider='aws'``.

    Adds nothing to the skeleton but the AWS identity; construct it with a
    :class:`~discovery.ingest.cloud_event_connector.CloudPollSource` (offline via
    :func:`build_offline_aws_source`, live via ``aws_poll_source.build_live_aws_source``).
    """

    provider = PROVIDER_AWS
    connector_id = "aws_events"

    def health_report(self) -> dict:
        """Per-account run-health (auth/throttle/partial states) — the R18-C2 panel
        artifact (AT-646). Empty when the poll source reports no health (offline)."""
        report = getattr(self.poll_source, "health_report", None)
        return report() if callable(report) else {}


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


def build_ingestor(
    org_id: str,
    *,
    env: Optional[Dict[str, str]] = None,
    poll_source: Optional[CloudPollSource] = None,
    raw_store: Optional[Any] = None,
    budget: Optional[int] = None,
    surfaces: Optional[Tuple[str, ...]] = None,
) -> Optional["AWSEventConnector"]:
    """Build the AWS Event Connector for ``org_id``, or None when unconfigured.

    The SINGLE entry point the discovery runner calls (mirroring
    ``azure_events.build_ingestor``), so "is AWS configured for this org?" is
    answered in exactly one place:

    * **Offline** (``INGEST_MODE`` is not ``live``) — always returns a connector
      over the deterministic event fixture, so an offline demo/run ingests real
      AWS-shaped events with no AWS account and no config (gap G9: offline mode
      previously had no production path at all).
    * **Live** — resolves the org's estate through
      :func:`discovery.ingest.aws_events_config.resolve_aws_event_config`
      (operator ``AWS_EVENT_ACCOUNTS`` override first, else the Owner-pinned
      accounts on the Integration Hub connector record) and polls exactly those
      accounts. Returns None when nothing is configured — a not-configured
      connector contributes nothing, which is not an error.

    Raises :class:`~discovery.ingest.aws_events_config.AWSEventConfigError` on a
    present-but-INVALID config, so a typo surfaces loudly instead of silently
    ingesting nothing. ``poll_source`` is injectable for tests.
    """
    from .aws_events_config import CONNECTOR_ID, resolve_aws_event_config

    try:
        from . import is_live
    except Exception:  # pragma: no cover - import shim
        from discovery.ingest import is_live  # type: ignore

    if budget is None:
        try:
            from discovery.signals.ops_calibration import CALIBRATED_RUN_EVENT_BUDGET

            budget = CALIBRATED_RUN_EVENT_BUDGET
        except Exception:  # pragma: no cover - calibration is advisory here
            budget = None

    if poll_source is None:
        if not is_live():
            logger.info(
                "aws_events: offline mode — polling the deterministic event fixture "
                "for org %s (no AWS account required)", org_id,
            )
            poll_source = build_offline_aws_source()
        else:
            config = resolve_aws_event_config(org_id, env=env)
            if config is None:
                logger.info(
                    "aws_events: not configured for org %s — no pinned accounts; "
                    "connect it in the Integration Hub.", org_id,
                )
                return None
            from .aws_auth import AWSAuthenticator
            from .aws_poll_source import AWSLivePollSource

            poll_source = AWSLivePollSource(
                config.accounts,
                AWSAuthenticator(),
                surfaces=tuple(surfaces) if surfaces else DEFAULT_POLL_SURFACES,
            )
            logger.info(
                "aws_events: live mode — %d pinned account(s) in partition %s for org %s",
                len(config.accounts), config.partition, org_id,
            )

    return AWSEventConnector(
        poll_source,
        connector_id=CONNECTOR_ID,
        raw_store=raw_store,
        budget=budget,
    )
