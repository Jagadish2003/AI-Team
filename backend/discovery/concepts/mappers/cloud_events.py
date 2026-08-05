"""2.0-B4 T2 — cloud event sources → normalised concepts.

AWS and Azure map exactly ONE concept: ``entity_reference``, pointing at the cloud
resource an event concerns. Every other concept is ``not_applicable`` for these
sources, and that is a deliberate boundary rather than missing work.

Why this module is four lines of mapping and a page of reasoning
---------------------------------------------------------------
MSP-B0 already normalised these sources. An ``OperationalEvent`` IS the correct
normalised shape for a cloud event, with its own closed vocabularies
(``EVENT_CLASSES``, ``SEVERITY_LEVELS``) and its own deterministic
``event_signature``. The temptation this module exists to refuse is re-expressing that
event as a workflow concept — an alarm as a ``work_item``, a resource state change as a
``state_transition`` — because both look superficially reasonable and both are wrong:

* a resource ``state_change`` and a work-item ``state_transition`` are read by
  DIFFERENT detectors with different semantics; an EC2 instance stopping is not a
  ticket moving to "On Hold", and giving them one concept would let an ageing detector
  measure the dwell time of a server;
* an alarm is not a tracked unit of work — nobody assigns it, it has no queue and no
  lifecycle a human drives. MSP-B7's ``ActiveSignal`` (dedup with an occurrence count)
  is what a re-firing alarm actually is.

So the B4 generalisation for these two sources is: keep using B0's profile, and take
from the concept set only the piece that genuinely crosses source families — the
reference to the resource, which is what lets a CMDB CI, an App Insights component and
a cloud resource be talked about in one vocabulary.

The reference is the bridge to MSP-B3 and 2.0-D3
------------------------------------------------
``entity_type='system'`` matches what ``discovery/signals/resource_graph.py`` writes
for an event-referenced resource, so a concept reference and the graph entity created
from the same event agree. That agreement is the whole point: it is what allows a
finding about a ServiceNow CI and a finding about an AWS resource to be recognised as
being about one thing (2.0-B2's cross-source resolution), instead of two.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from discovery.concepts import model as m
    from discovery.concepts.mappers import maps
    from discovery.concepts.mappers._common import MappingInputError, require_id, text
except ModuleNotFoundError:  # pragma: no cover - import-style shim
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.mappers import maps
    from backend.discovery.concepts.mappers._common import MappingInputError, require_id, text


def _resource_reference(source_system: str, event: Any) -> m.EntityReference:
    """The resource an ``OperationalEvent`` (or its dict form) concerns.

    Accepts either an ``OperationalEvent`` or the dict it serialises to, because the
    callers on both sides of the ingest boundary hold different forms — the same
    tolerance ``reference_mappers`` applies to provider payloads.

    An event with NO resource yields an error rather than a reference to the account or
    the region. B0's ``resource`` is optional precisely because some events concern no
    resource, and inventing a stand-in would create a graph entity for a thing that
    does not exist — the speculative estate modelling ``resource_graph.py`` refuses.
    """
    resource = getattr(event, "resource", None)
    if resource is None and isinstance(event, dict):
        resource = event.get("resource")
    if resource is None:
        raise MappingInputError(
            "this event references no cloud resource, so there is nothing to point at; "
            "an account- or region-level stand-in would invent an entity"
        )

    resource_id = getattr(resource, "resource_id", None)
    display = getattr(resource, "name", None)
    if resource_id is None and isinstance(resource, dict):
        resource_id = resource.get("resource_id")
        display = resource.get("name")

    return m.EntityReference(
        entity_type="system",
        source_system=source_system,
        source_record_id=require_id(resource_id, "cloud resource id"),
        display_name=text(display),
    )


@maps("aws_events", m.CONCEPT_ENTITY_REFERENCE)
def map_aws_resource_reference(event: Any) -> m.EntityReference:
    """An AWS operational event's resource → :class:`EntityReference`.

    Keyed on the ARN, which is AWS's own stable identifier — so the reference resolves
    to the same resource whatever surface (CloudWatch, EventBridge, CloudTrail, or the
    B8 bridge) reported the event.
    """
    return _resource_reference("aws", event)


@maps("azure_events", m.CONCEPT_ENTITY_REFERENCE)
def map_azure_resource_reference(event: Any) -> m.EntityReference:
    """An Azure operational event's resource → :class:`EntityReference`.

    Keyed on the Azure resource id. Case is preserved verbatim rather than folded:
    ``source_record_id`` is a passthrough by contract, and normalising it would break
    trace-back to the record as the provider reported it. Azure resource ids being
    case-insensitive is a matching concern for the RESOLVER, not a licence for a mapper
    to rewrite an id.
    """
    return _resource_reference("azure", event)


def resource_reference_or_none(source_system: str, event: Any) -> Optional[m.EntityReference]:
    """:func:`_resource_reference` for a caller mapping a whole event stream.

    Returns ``None`` for a resourceless event instead of raising, so one such event does
    not abort a batch. Provided as a SEPARATE function rather than a flag on the
    mappers: a caller has to choose tolerance explicitly, so a strict caller cannot get
    it by accident.
    """
    try:
        return _resource_reference(source_system, event)
    except (MappingInputError, ValueError):
        return None


__all__ = [
    "map_aws_resource_reference",
    "map_azure_resource_reference",
    "resource_reference_or_none",
]
