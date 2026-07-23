"""MSP-B1 / AT-645 (T5) — partition-aware AWS endpoint configuration.

AWS is not one cloud: the **commercial** partition (``aws``) and **GovCloud**
(``aws-us-gov``) are isolated partitions with their own regions, STS behaviour, and
ARN namespaces. An MSP serving a public-sector customer connects to GovCloud, and
the endpoints a connector talks to differ by partition. This module makes that
partition-aware **from day one** — the endpoint resolution is a small, pure,
config-level surface (no network, no boto3) so it is exhaustively testable and so
MSP-B9's LIVE verification has a stable surface to consume rather than a retrofit.

What differs by partition (V1 scope: ``aws`` and ``aws-us-gov`` only)
---------------------------------------------------------------------
* **Regions** — GovCloud regions are ``us-gov-west-1`` / ``us-gov-east-1``; a
  commercial region under a GovCloud connection (or vice-versa) is a
  misconfiguration and is rejected at config time (:func:`validate_region`).
* **STS** — the commercial partition has a *global* ``sts.amazonaws.com``
  endpoint; GovCloud has regional STS only. Role assumption must target the right
  one.
* **ARN partition** — GovCloud resource ARNs are ``arn:aws-us-gov:…``, not
  ``arn:aws:…``. The connector stamps the correct partition when it builds a
  resource ARN (e.g. a CloudWatch alarm ARN).

Both partitions share the ``amazonaws.com`` DNS suffix (China's ``aws-cn`` /
``amazonaws.com.cn`` is deliberately out of V1 scope). The service endpoint host
is ``{service-prefix}.{region}.{dns_suffix}`` — note CloudWatch's API prefix is
``monitoring`` and EventBridge's is ``events``.

Selectable per connection
-------------------------
:class:`~discovery.ingest.aws_auth.AWSAccountConfig` carries an optional
``partition``; when unset it is derived from the account's region(s), so an
operator can either state it explicitly or let a GovCloud region imply it — and a
region that contradicts the stated partition fails loudly.

B9 follow-through
-----------------
This is the CONFIG surface only. MSP-B9's live-verification item exercises a real
GovCloud connection end-to-end against these resolved endpoints; see
:data:`B9_LIVE_VERIFICATION_NOTE`. FIPS endpoint variants (a common GovCloud
compliance requirement) are also deferred to that live-verification follow-through.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Partition identities
# ─────────────────────────────────────────────────────────────────────────────

PARTITION_AWS = "aws"                 # commercial
PARTITION_GOVCLOUD = "aws-us-gov"     # GovCloud (US)

#: GovCloud region prefix — the load-bearing discriminator between the partitions.
_GOVCLOUD_REGION_PREFIX = "us-gov-"

#: The AWS services the native connector talks to, and their endpoint host prefix
#: (CloudWatch's API endpoint is ``monitoring``; EventBridge's is ``events``).
SERVICE_ENDPOINT_PREFIXES: Dict[str, str] = {
    "cloudwatch": "monitoring",
    "events": "events",
    "cloudtrail": "cloudtrail",
    "sts": "sts",
}

#: The services enumerated by :func:`endpoint_map`.
CONNECTOR_SERVICES = ("cloudwatch", "events", "cloudtrail", "sts")

#: Explicit reference to the MSP-B9 follow-through (AT-645 AC7): this module is the
#: config surface; B9 verifies it live against a real GovCloud connection.
B9_LIVE_VERIFICATION_NOTE = (
    "MSP-B9 live verification consumes this partition config surface: it connects "
    "to a real aws-us-gov (GovCloud) account and confirms the resolved GovCloud "
    "endpoints (monitoring/events/cloudtrail/sts) and ARN partition work "
    "end-to-end. FIPS endpoint variants are handled as part of that follow-through."
)


class PartitionError(ValueError):
    """Raised for an unknown partition or a region that contradicts its partition."""


@dataclass(frozen=True)
class AWSPartition:
    """Static description of an AWS partition (pure config — no network/boto3)."""

    partition_id: str      # also the ARN partition ('aws' / 'aws-us-gov')
    dns_suffix: str        # endpoint DNS suffix (both V1 partitions: amazonaws.com)
    sts_global: bool       # commercial has a global STS endpoint; GovCloud does not


_PARTITIONS: Dict[str, AWSPartition] = {
    PARTITION_AWS: AWSPartition(PARTITION_AWS, "amazonaws.com", sts_global=True),
    PARTITION_GOVCLOUD: AWSPartition(PARTITION_GOVCLOUD, "amazonaws.com", sts_global=False),
}

ALL_PARTITIONS = tuple(_PARTITIONS)


# ─────────────────────────────────────────────────────────────────────────────
# Resolution + validation
# ─────────────────────────────────────────────────────────────────────────────

def partition_for(partition_id: str) -> AWSPartition:
    """Return the :class:`AWSPartition`, raising :class:`PartitionError` if unknown."""
    try:
        return _PARTITIONS[partition_id]
    except KeyError:
        raise PartitionError(
            f"unknown AWS partition {partition_id!r}; expected one of {list(ALL_PARTITIONS)}"
        )


def is_govcloud_region(region: Optional[str]) -> bool:
    """True when ``region`` is a GovCloud region (``us-gov-*``)."""
    return bool(region) and str(region).lower().startswith(_GOVCLOUD_REGION_PREFIX)


def resolve_partition_for_region(region: Optional[str]) -> str:
    """Derive the partition id from a region (``us-gov-*`` → GovCloud, else commercial)."""
    return PARTITION_GOVCLOUD if is_govcloud_region(region) else PARTITION_AWS


def arn_partition_for_region(region: Optional[str]) -> str:
    """The ARN partition token for a region (``aws`` / ``aws-us-gov``)."""
    return resolve_partition_for_region(region)


def validate_region(partition_id: str, region: Optional[str]) -> None:
    """Assert ``region`` belongs to ``partition_id`` (raise :class:`PartitionError`).

    An empty region is allowed (the client falls back to its default region). A
    GovCloud region under the commercial partition — or a commercial region under
    GovCloud — is a misconfiguration surfaced at config time, not a silent
    mis-endpoint at run time.
    """
    partition_for(partition_id)  # validates the partition id itself
    if not region:
        return
    gov_region = is_govcloud_region(region)
    if partition_id == PARTITION_GOVCLOUD and not gov_region:
        raise PartitionError(
            f"region {region!r} is not a GovCloud (us-gov-*) region but partition "
            f"is {PARTITION_GOVCLOUD!r}"
        )
    if partition_id == PARTITION_AWS and gov_region:
        raise PartitionError(
            f"GovCloud region {region!r} configured under the commercial partition "
            f"{PARTITION_AWS!r}; set partition to {PARTITION_GOVCLOUD!r}"
        )


def resolve_endpoint(partition_id: str, service: str, region: Optional[str]) -> str:
    """Resolve the endpoint URL for ``service`` in ``partition_id`` / ``region``.

    ``{prefix}.{region}.{dns_suffix}`` for a regional service. STS with no region
    resolves to the commercial *global* endpoint (``sts.amazonaws.com``); GovCloud
    has no global STS, so a region is required there.
    """
    partition = partition_for(partition_id)
    prefix = SERVICE_ENDPOINT_PREFIXES.get(service, service)
    if service == "sts" and not region:
        if partition.sts_global:
            return f"https://sts.{partition.dns_suffix}"
        raise PartitionError(
            f"{partition_id!r} has no global STS endpoint; a region is required"
        )
    if not region:
        raise PartitionError(f"a region is required to resolve the {service!r} endpoint")
    return f"https://{prefix}.{region}.{partition.dns_suffix}"


def resolve_service_endpoint_or_none(service: str, region: Optional[str]) -> Optional[str]:
    """Best-effort endpoint for the client factory — ``None`` to let boto3 default.

    Derives the partition from the region. Returns ``None`` when the service is not
    one this connector configures, or when no endpoint can be resolved (e.g. no
    region for a non-STS service) — in which case boto3's own endpoint resolution
    is used unchanged.
    """
    if service not in SERVICE_ENDPOINT_PREFIXES:
        return None
    partition_id = resolve_partition_for_region(region)
    try:
        return resolve_endpoint(partition_id, service, region)
    except PartitionError:
        return None


def endpoint_map(partition_id: str, region: str) -> Dict[str, str]:
    """The full connector endpoint map for a partition/region (the AC7 surface).

    Resolves every service the connector uses, so a config-level test can assert
    the GovCloud endpoints resolve correctly without any live call.
    """
    validate_region(partition_id, region)
    return {
        service: resolve_endpoint(partition_id, service, region)
        for service in CONNECTOR_SERVICES
    }
