"""MSP-B0 / AT-638 — raw-payload storage + evidence-pointer resolution.

A normalised :class:`~discovery.signals.operational_event.OperationalEvent` is
what detectors, scoring, and reporting consume — deliberately provider-agnostic
and stripped of the raw provider payload (the "one model for every cloud"
guarantee). But an analyst auditing a finding must still be able to reach the
*original* provider event it came from. This module is the bridge:

* the **raw-event store** (:class:`RawEventStore`) persists each raw provider
  payload, hard-partitioned by ``org_id`` (T4-AC3);
* every normalised event already carries an OBSERVED **evidence pointer** (the
  R16-B1 :class:`~app.provenance.EvidencePointer` minted by
  ``OperationalEvent.build``) whose ``(source_system, source_artifact)`` is the
  store key (T4-AC1);
* :func:`resolve_raw_event` walks that pointer back to the stored raw payload,
  refusing to cross an org boundary (T4-AC2 / T4-AC3).

The detector-visible model never embeds the raw payload — the raw JSON lives
ONLY in the store and is reachable ONLY through the pointer (T4-AC4). The store
is an interface with an in-memory default here; a DB-backed implementation drops
in for live ingestion (B1/B2/B8) without touching callers.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from app.provenance import EvidencePointer
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.provenance import EvidencePointer

from .operational_event import OperationalEvent

_Key = Tuple[str, str, str]  # (org_id, source_system, source_artifact)


class OrgScopeError(RuntimeError):
    """Raised when an evidence operation would cross an organization boundary.

    Evidence is hard-partitioned by org (T4-AC3); an attempt to store or resolve
    a raw payload under a different org than the event owns is a programming
    error, not a silent miss, so it fails loudly.
    """


def _require_org(org_id: str) -> str:
    if not org_id:
        raise OrgScopeError("org_id is required for every evidence operation")
    return str(org_id)


def _pointer(event: OperationalEvent) -> EvidencePointer:
    return EvidencePointer.from_dict(event.provenance)


# ─────────────────────────────────────────────────────────────────────────────
# Raw-event store (the evidence storage layer)
# ─────────────────────────────────────────────────────────────────────────────

class RawEventStore(ABC):
    """Org-partitioned persistence for raw provider payloads.

    Keyed by ``(org_id, source_system, source_artifact)`` — exactly the tuple an
    event's evidence pointer carries — so a pointer resolves to its payload with
    no extra bookkeeping. Implementations MUST isolate orgs: a ``get`` under a
    different org than a payload was stored under never returns it.
    """

    @abstractmethod
    def put(self, org_id: str, source_system: str, source_artifact: str,
            raw_payload: Dict[str, Any]) -> None:
        ...

    @abstractmethod
    def get(self, org_id: str, source_system: str,
            source_artifact: str) -> Optional[Dict[str, Any]]:
        ...


class InMemoryRawEventStore(RawEventStore):
    """In-memory :class:`RawEventStore` — the offline/default implementation.

    Deep-copies on both ``put`` and ``get`` so a stored payload can never be
    mutated through an aliased reference held by a caller. Deterministic and
    dependency-free; suitable for fixtures and tests. Live ingestion swaps in a
    DB-backed store with the same interface.
    """

    def __init__(self) -> None:
        self._data: Dict[_Key, Dict[str, Any]] = {}

    def put(self, org_id: str, source_system: str, source_artifact: str,
            raw_payload: Dict[str, Any]) -> None:
        org_id = _require_org(org_id)
        if not isinstance(raw_payload, dict):
            raise ValueError("raw_payload must be a dict")
        self._data[(org_id, str(source_system), str(source_artifact))] = copy.deepcopy(raw_payload)

    def get(self, org_id: str, source_system: str,
            source_artifact: str) -> Optional[Dict[str, Any]]:
        org_id = _require_org(org_id)
        found = self._data.get((org_id, str(source_system), str(source_artifact)))
        return copy.deepcopy(found) if found is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# Store + resolve (evidence-pointer resolution)
# ─────────────────────────────────────────────────────────────────────────────

def store_raw_event(
    store: RawEventStore,
    org_id: str,
    event: OperationalEvent,
    raw_payload: Dict[str, Any],
) -> OperationalEvent:
    """Persist ``raw_payload`` under the event's evidence pointer (T4-AC1/AC2).

    The event already carries the pointer (``OperationalEvent.build`` minted it);
    this stores the raw payload at the pointer's key so the pointer *resolves*.
    Refuses to store under an org other than the one the event belongs to
    (T4-AC3). Returns the event unchanged for call-site chaining.
    """
    org_id = _require_org(org_id)
    if event.org_id != org_id:
        raise OrgScopeError(
            f"event belongs to org {event.org_id!r}, cannot store under {org_id!r}"
        )
    p = _pointer(event)
    store.put(org_id, p.source_system, p.source_artifact, raw_payload)
    return event


def resolve_raw_event(
    store: RawEventStore,
    org_id: str,
    event: OperationalEvent,
) -> Optional[Dict[str, Any]]:
    """Resolve a normalised event back to its stored raw payload (T4-AC2).

    Walks the event's evidence pointer ``(source_system, source_artifact)`` and
    reads the raw payload from the store within ``org_id``. Refuses to resolve
    across an org boundary (T4-AC3); returns ``None`` when nothing is stored for
    the pointer (e.g. raw persistence was skipped or already purged).
    """
    org_id = _require_org(org_id)
    if event.org_id != org_id:
        raise OrgScopeError(
            f"event belongs to org {event.org_id!r}, cannot resolve under {org_id!r}"
        )
    p = _pointer(event)
    return store.get(org_id, p.source_system, p.source_artifact)


def map_and_store(
    mapper: Callable[..., OperationalEvent],
    raw_payload: Dict[str, Any],
    *,
    org_id: str,
    store: RawEventStore,
) -> OperationalEvent:
    """Map a raw provider payload and persist it in one step (connector entry point).

    Runs the reference ``mapper`` (see :mod:`discovery.signals.reference_mappers`)
    to produce the normalised event, then stores the raw payload against its
    evidence pointer — so the returned event both hides the raw payload from
    detectors (T4-AC4) and resolves back to it (T4-AC2).
    """
    org_id = _require_org(org_id)
    event = mapper(raw_payload, org_id=org_id)
    return store_raw_event(store, org_id, event, raw_payload)
