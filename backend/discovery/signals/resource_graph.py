"""MSP-B0 / AT-639 — resource entities into the knowledge graph (conservative).

When an operational event references a cloud resource, that resource becomes a
graph entity so downstream discovery (relationship mapping, graph context,
correlation) can reason about it. The creation rule is deliberately
**event-driven and conservative**:

* a resource becomes an entity **only** when an observed event references it
  (T5-AC1 / T5-AC2) — we never invent a resource we have not seen in a signal;
* **no speculative estate modelling** (T5-AC3) — we do NOT infer parent/child
  infrastructure, sibling resources, or an estate topology from a single event.
  Building the full estate map is B3's CMDB job; B0 only promotes the resources
  that events actually name. This module draws *nodes*, never speculative edges.

Each resource is created through the existing conservative entity resolver
(`app.entity_resolution.resolve_or_create_entity`), so it lands in the standard
`entities` table — org-scoped, resolved, carrying an OBSERVED evidence pointer —
and is immediately usable by every downstream graph consumer (T5-AC4). Cloud
resources are modelled as ``entity_type='system'`` (they are infrastructure
systems in the estate) and keyed on their globally-unique provider id
(ARN / Azure resource id) as ``source_record_id``, so repeat sightings of one
resource always resolve to a single node and two distinct resources never
false-merge.

The resolver is injectable for testing; the default is resolved lazily so
importing :mod:`discovery.signals` stays dependency-light (no ``app.db`` import
until a resource is actually persisted).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from .operational_event import OperationalEvent

logger = logging.getLogger(__name__)

#: Cloud resources are infrastructure systems in the estate — modelled with the
#: locked ``system`` entity type (never a new type; the entity schema is locked).
CLOUD_RESOURCE_ENTITY_TYPE = "system"


def _default_resolver() -> Callable[..., Any]:
    """Lazily import the core entity resolver (keeps the package import light)."""
    try:
        from app.entity_resolution import resolve_or_create_entity
    except ModuleNotFoundError:  # project-root execution uses backend as package
        from backend.app.entity_resolution import resolve_or_create_entity
    return resolve_or_create_entity


def _resource_metadata(event: OperationalEvent) -> Dict[str, Any]:
    """Build the graph-entity metadata for a resource an event referenced.

    Records what the resource IS (provider / normalised resource_type / region /
    friendly name) and how it was OBSERVED (the source surface + the event's
    recurrence signature) so downstream consumers — and, later, B3's CMDB — can
    tell an event-observed resource from a speculatively-modelled one.
    """
    res = event.resource
    return {
        "cloud_resource": True,          # marks an event-observed estate node
        "provider": res.provider,
        "resource_type": res.resource_type,
        "region": res.region,
        "resource_name": res.name,
        "observed_via": event.source_system,
        "event_signature": event.event_signature,
    }


def create_resource_entities(
    events: Iterable[OperationalEvent],
    *,
    run_id: str,
    resolver: Optional[Callable[..., Any]] = None,
) -> List[Any]:
    """Create/resolve graph entities for the resources events reference.

    Event-driven and conservative (T5-AC2/AC3): an entity is created ONLY for a
    resource an event actually references — events with no ``resource`` (or an
    empty resource id) contribute nothing, and no related/speculative resources
    are inferred. Distinct resources are de-duplicated per ``(org_id,
    resource_id)`` so one resolver call is made per resource regardless of how
    many events reference it. Org isolation rides on each event's ``org_id``.

    Args:
        events:   the normalised operational events to process.
        run_id:   the current discovery run id (for entity first/last-seen).
        resolver: entity resolver (injectable for tests); defaults to the core
                  conservative resolver.

    Returns:
        The resolved/created resource entities (de-duplicated, in first-seen
        order). Never raises — a per-resource failure is logged and skipped so
        graph promotion never breaks a run.
    """
    if resolver is None:
        resolver = _default_resolver()

    entities: List[Any] = []
    seen: set[tuple[str, str]] = set()

    for event in events:
        if not isinstance(event, OperationalEvent):
            continue
        res = event.resource
        # Conservative: no referenced resource → no entity (T5-AC2/AC3).
        if res is None or not res.resource_id:
            continue
        key = (event.org_id, res.resource_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            entity = resolver(
                org_id=event.org_id,
                entity_type=CLOUD_RESOURCE_ENTITY_TYPE,
                # The globally-unique provider id is the display+identity key, so
                # two distinct resources can never false-merge on a shared name;
                # the friendly name is kept in metadata for display.
                display_name=res.resource_id,
                source_system=res.provider,
                source_record_id=res.resource_id,
                run_id=run_id,
                metadata=_resource_metadata(event),
            )
            if entity is not None:
                entities.append(entity)
        except Exception as exc:  # noqa: BLE001 — graph promotion is non-blocking
            logger.warning(
                "resource entity creation failed for %s (%s): %s",
                res.resource_id, res.provider, exc,
            )

    logger.info(
        "resource graph — run=%s referenced_resources=%d entities=%d",
        run_id, len(seen), len(entities),
    )
    return entities
