"""
security_ops_evidence_resolver.py — MSP-B12 T3: org-scoped, access-controlled,
audited resolution of a Security-Operations evidence pointer to its individual
source record.

The aggregation floor (``security_ops_aggregation_floor``) guarantees no pack
OUTPUT enumerates individual records. This module is the ONLY sanctioned path back
to an individual record: given an evidence pointer carried on a finding's
source_trace, an AUTHORIZED analyst in the OWNING organization can resolve exactly
ONE record at a time, and every resolution attempt leaves an audit trail.

Guarantees (MSP-B12 T3 / AC2):
  * Org-scoped — records live in a hard org-partitioned store; a pointer resolves
    only within the requesting org's partition, so a user from another org can
    never resolve it (the record simply is not in their partition).
  * Access-controlled — a minimum role (analyst) is required; a viewer or an
    unauthenticated caller is denied.
  * Audited — every attempt (resolved OR denied) emits a
    ``secops.evidence_pointer_resolved`` event carrying the requesting org, user,
    source system, pointer identifier, and access time.
  * Lean pointers — the pointer on a finding names only the source artifact and
    provenance; the sensitive record content lives ONLY in the store and is
    returned solely to an authorized, audited resolution.

The store is an injectable abstraction; the in-memory implementation here is the
default (and drops in a DB-backed store for production). Resolution never depends
on wall-clock nondeterminism: the audit clock is injectable.
"""
from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

try:  # package-qualified first, bare fallback
    from backend.app.provenance import EvidencePointer
except ModuleNotFoundError:  # pragma: no cover - import shim
    from app.provenance import EvidencePointer

AUDIT_EVENT = "secops.evidence_pointer_resolved"

# Role hierarchy — mirrors app.rbac._ROLE_RANK (owner > analyst > viewer). Kept
# local so the resolver is a pure, FastAPI-free service (a route passes the role
# resolved from require_role/require_auth).
_ROLE_RANK: Dict[str, int] = {"owner": 3, "analyst": 2, "viewer": 1}
DEFAULT_MIN_ROLE = "analyst"


def role_at_least(role: Optional[str], minimum: str) -> bool:
    """True when ``role`` ranks at or above ``minimum`` (owner>analyst>viewer)."""
    return _ROLE_RANK.get((role or "").strip().lower(), 0) >= _ROLE_RANK.get(minimum, 99)


# ── Outcomes ─────────────────────────────────────────────────────────────────


OUTCOME_RESOLVED = "resolved"
OUTCOME_DENIED = "denied"

REASON_INSUFFICIENT_ROLE = "insufficient_role"
REASON_NOT_FOUND_OR_CROSS_ORG = "not_found_or_cross_org"
REASON_INVALID_POINTER = "invalid_pointer"


class EvidenceAccessError(Exception):
    """Base for a denied Security-Operations evidence resolution."""

    reason: str = ""


class EvidenceAccessDenied(EvidenceAccessError):
    """The caller is not permitted to resolve the pointer (role / org / missing)."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ── Org-partitioned source-record store ──────────────────────────────────────


class EvidenceRecordStore(ABC):
    """Hard org-partitioned store of individual source records, keyed by the exact
    ``(org_id, source_system, source_artifact)`` tuple an evidence pointer carries.
    """

    @abstractmethod
    def put(self, org_id: str, source_system: str, source_artifact: str, record: Dict[str, Any]) -> None: ...

    @abstractmethod
    def get(self, org_id: str, source_system: str, source_artifact: str) -> Optional[Dict[str, Any]]: ...


class InMemoryEvidenceRecordStore(EvidenceRecordStore):
    """Default in-memory store. Deep-copies on put/get so a caller can never mutate
    stored state, and partitions strictly by org so cross-org reads miss."""

    def __init__(self) -> None:
        self._by_org: Dict[str, Dict[tuple, Dict[str, Any]]] = {}

    def put(self, org_id: str, source_system: str, source_artifact: str, record: Dict[str, Any]) -> None:
        if not (org_id and source_system and source_artifact):
            return
        self._by_org.setdefault(org_id, {})[(source_system, source_artifact)] = copy.deepcopy(record)

    def get(self, org_id: str, source_system: str, source_artifact: str) -> Optional[Dict[str, Any]]:
        partition = self._by_org.get(org_id)
        if not partition:
            return None
        record = partition.get((source_system, source_artifact))
        return copy.deepcopy(record) if record is not None else None


# ── Populate the store from a run's normalized B11 signal ────────────────────


_INDEXED_COLLECTIONS = (
    ("secops", "security_incidents"),
    ("vulnerability_response", "vulnerable_items"),
    ("vulnerability_response", "vulnerability_groups"),
    ("vulnerability_response", "remediation_tasks"),
)


def index_signal_records(store: EvidenceRecordStore, org_id: str, sn_data: Optional[Dict[str, Any]]) -> int:
    """Index every MSP-B11 workflow record into the org-partitioned store.

    Keyed by ``('servicenow', sys_id)`` — the same tuple the detectors' evidence
    pointers carry — so a finding's pointer resolves to its record. Returns the
    number of records indexed. Records are already B11-field-scoped (no scanner /
    exploit / CVE content), so the store never holds the truly sensitive payload.
    """
    if not org_id or not isinstance(sn_data, dict):
        return 0
    indexed = 0
    for block_key, collection_key in _INDEXED_COLLECTIONS:
        block = sn_data.get(block_key)
        if not isinstance(block, dict):
            continue
        for record in block.get(collection_key) or []:
            if not isinstance(record, dict):
                continue
            sys_id = record.get("sys_id") or record.get("id")
            if not sys_id:
                continue
            store.put(org_id, "servicenow", str(sys_id), record)
            indexed += 1
    return indexed


# ── Resolution (org-scoped, access-controlled, audited) ──────────────────────


def _pointer_dict(pointer: Any) -> Optional[Dict[str, Any]]:
    """Accept a raw pointer dict, a source_trace artifact that wraps one, or an
    EvidencePointer; return the pointer dict (or None if not resolvable)."""
    if isinstance(pointer, EvidencePointer):
        return pointer.to_dict()
    if isinstance(pointer, dict):
        for key in ("evidence_pointer", "pointer", "provenance"):
            nested = pointer.get(key)
            if isinstance(nested, dict):
                return nested
        if pointer.get("source_system") and pointer.get("source_artifact"):
            return pointer
    return None


def resolve_evidence_pointer(
    pointer: Any,
    *,
    requesting_org: str,
    user_id: Optional[str],
    role: Optional[str],
    store: EvidenceRecordStore,
    min_role: str = DEFAULT_MIN_ROLE,
    emit: Optional[Callable[[str, dict], None]] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> Dict[str, Any]:
    """Resolve one evidence pointer to its individual source record, audited.

    Enforces role (``min_role``, default analyst) and org scope (the record must
    live in ``requesting_org``'s partition), emits a ``secops.evidence_pointer_resolved``
    audit event for the attempt, and returns ``{"provenance", "record"}`` on
    success. Raises :class:`EvidenceAccessDenied` (auditing the denial) otherwise.
    """
    emit = emit or _default_emit
    access_time = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()

    ptr = _pointer_dict(pointer)
    parsed = EvidencePointer.from_dict(ptr) if ptr else None
    if parsed is None or not parsed.is_valid():
        _audit(emit, requesting_org, user_id, ptr, OUTCOME_DENIED, REASON_INVALID_POINTER, access_time)
        raise EvidenceAccessDenied("evidence pointer is missing or invalid", reason=REASON_INVALID_POINTER)

    source_system = parsed.source_system
    pointer_id = parsed.source_artifact

    # Access control — a viewer or unauthenticated caller cannot resolve.
    if not role_at_least(role, min_role):
        _audit(emit, requesting_org, user_id, ptr, OUTCOME_DENIED, REASON_INSUFFICIENT_ROLE, access_time)
        raise EvidenceAccessDenied(
            f"role {role!r} may not resolve evidence pointers (requires {min_role}+)",
            reason=REASON_INSUFFICIENT_ROLE,
        )

    # Org scope — resolution only within the requesting org's partition. A pointer
    # to another org's record simply is not present here → denied.
    record = store.get(requesting_org, source_system, pointer_id) if requesting_org else None
    if record is None:
        _audit(emit, requesting_org, user_id, ptr, OUTCOME_DENIED, REASON_NOT_FOUND_OR_CROSS_ORG, access_time)
        raise EvidenceAccessDenied(
            "evidence pointer does not resolve within the requesting organization",
            reason=REASON_NOT_FOUND_OR_CROSS_ORG,
        )

    _audit(emit, requesting_org, user_id, ptr, OUTCOME_RESOLVED, None, access_time)
    return {
        "provenance": {
            "source_system": source_system,
            "source_artifact": pointer_id,
            "source_timestamp": parsed.source_timestamp,
            "origin": parsed.origin,
            "source_url": record.get("source_url"),
        },
        "record": record,
        "resolved": True,
        "access_time": access_time,
    }


def _audit(
    emit: Callable[[str, dict], None],
    org_id: str,
    user_id: Optional[str],
    ptr: Optional[Dict[str, Any]],
    outcome: str,
    reason: Optional[str],
    access_time: str,
) -> None:
    payload: Dict[str, Any] = {
        "org_id": org_id or "",
        "user_id": user_id or "",
        "source_system": (ptr or {}).get("source_system", ""),
        "pointer_id": (ptr or {}).get("source_artifact", ""),
        "outcome": outcome,
        "access_time": access_time,
    }
    if reason:
        payload["reason"] = reason
    try:
        emit(AUDIT_EVENT, payload)
    except Exception:  # pragma: no cover - audit must never break resolution flow
        pass


def _default_emit(event_type: str, payload: dict) -> None:
    """Emit via the telemetry write API (best-effort, lazily imported)."""
    try:
        from app.telemetry import record_event
    except ModuleNotFoundError:  # pragma: no cover
        from backend.app.telemetry import record_event
    record_event(event_type, payload)
