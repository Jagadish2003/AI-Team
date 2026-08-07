"""MSP-B4 T5 / AT-666 — soft enrichment joins for recurrence findings.

A :class:`~discovery.detectors.ops_recurrence.RecurrenceRecord` answers *"this
same loop happened N times, always resolved the same way"*. T5 layers two
OPTIONAL, CONSERVATIVE provenance hops on top of it so the finding can, when the
upstream data exists, show a fuller story:

    recurrence  →  incident                         (always present: the finding's own examples)
    incident    →  CI / CI-class                    (B3 soft join — "located" when CMDB is present)
    event_sig   →  incident  →  resolution          (B0/B7 soft join — "linked" when the event
                                                      bridge already tied alerts to incidents)

Both joins are **soft**. A recurrence that cannot be located against a CI, or
linked to an operational event signature, is still emitted — unenriched, never
suppressed and never failed (AC5). Every hop is recorded on the evidence trace
with a status that distinguishes:

* ``not_available`` — the upstream dependency was absent (no B3 CMDB data; no
  event-signature link on any incident). "We never had the data."
* ``join_failed``  — the dependency WAS present but the specific join could not
  be made (an incident named a CI that does not resolve in the scoped CMDB).
  "The data was there but this join did not land."
* ``joined``       — the hop landed and carries its provenance.

Consumers must be able to tell "not available" from "join failed"; a silently
missing hop would read as *no relationship exists* when the truth is *the join
was attempted and did not resolve*.

Conservative discipline (the same instinct as B0's ``event_signature`` and B4's
``resolution_signature``): the CI join is a **deterministic sys_id lookup**, and
the event join records **only an explicit deterministic alert→incident link the
upstream bridge already stamped** on the incident. Neither join does text- or
timing-based matching — that similarity work belongs to MSP-B5/B7, not here. If
this join starts guessing, it has stopped being evidence.

Groups, never people (AC4): the only identities that ever cross this boundary
are CI classes / CI (system) names and assignment groups — no individual is
named in any join output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.provenance import EvidencePointer

# ── join / hop vocabulary ────────────────────────────────────────────────────

#: The specific join landed and carries provenance.
STATUS_JOINED = "joined"
#: The upstream dependency was absent — the join was never attemptable.
STATUS_NOT_AVAILABLE = "not_available"
#: The dependency was present but this specific join did not resolve.
STATUS_JOIN_FAILED = "join_failed"

#: The three evidence-trace hops, in loop order.
HOP_RECURRENCE_TO_INCIDENT = "recurrence_to_incident"
HOP_INCIDENT_TO_CI = "incident_to_ci"
HOP_EVENT_TO_INCIDENT_TO_RESOLUTION = "event_signature_to_incident_to_resolution"

#: Incident (or resolution-block) fields that may carry the explicit, upstream-
#: established event-signature link. A list field is preferred; the singular
#: field is accepted for a one-alert incident.
#:
#: These are SERVICENOW COLUMN names, read off an incident payload — not AgentIQ
#: structure names. The ServiceNow instance stores the link in the scoped-
#: application field ``x_1212781_github_0_event_signatures``, which is what the
#: incident query requests (``servicenow.INCIDENT_EVENT_SIGNATURE_FIELDS``) and
#: what the incident payload carries through verbatim. The internal AgentIQ key
#: ``block["event_signatures"]`` is a DIFFERENT thing and is deliberately
#: unchanged — nothing here reads or renames it.
#: The SAME column is named in both tuples deliberately: ServiceNow returns a
#: multi-valued field as a list but a single-valued one as a plain string, so the
#: list branch handles the former and the scalar branch the latter. No invented
#: sibling column name is used — only the field the instance actually defines.
EVENT_SIGNATURE_LIST_FIELDS: Tuple[str, ...] = (
    "x_1212781_github_0_event_signatures",
)
EVENT_SIGNATURE_SCALAR_FIELDS: Tuple[str, ...] = (
    "x_1212781_github_0_event_signatures",
)

#: An event signature is ``"{version}:{sha256_128bit_hex}"`` (see
#: :mod:`discovery.signals.event_signature`). We accept ONLY strings of that
#: exact shape as an explicit link, so arbitrary free text can never be mistaken
#: for a deterministic relationship (conservative-by-construction).
_EVENT_SIGNATURE_RE = re.compile(r"^\d+:[0-9a-f]{32}$")

_POINTER_FIELDS = frozenset(EvidencePointer.__dataclass_fields__)


def _text(value: Any) -> Optional[str]:
    """Reduce a scalar / ServiceNow reference object to a trimmed string."""
    if isinstance(value, Mapping):
        value = (
            value.get("value")
            or value.get("display_value")
            or value.get("displayName")
            or value.get("name")
        )
    if value is None:
        return None
    result = " ".join(str(value).strip().split())
    return result or None


def _safe_pointer(value: Any) -> Optional[Dict[str, Any]]:
    """Allow-list the shared evidence spine; never pass source payloads through."""
    if not isinstance(value, Mapping):
        return None
    pointer = EvidencePointer.from_dict(
        {key: value.get(key) for key in _POINTER_FIELDS if key in value}
    )
    return pointer.to_dict() if pointer.is_valid() else None


# ── B3 CMDB index (incident CI reference → CI class) ─────────────────────────

def build_cmdb_index(
    sn_data: Optional[Mapping[str, Any]],
    *,
    org_id: Optional[str] = None,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Index the run's B3 CMDB CIs by ``sys_id`` — or ``None`` when B3 is absent.

    Returns ``None`` when no ``cmdb`` block is present (B3 not available — the
    soft-dependency-absent case, AC5). Returns a dict (possibly empty) when the
    block IS present, so a caller can distinguish "no B3" (``None``) from "B3
    present but this CI did not resolve" (a miss against a non-``None`` index).

    Org-scoped (AC7): a CMDB block whose ``org_id`` names a different org than
    the run is treated as not-available for this run.
    """
    cmdb = (sn_data or {}).get("cmdb")
    if not isinstance(cmdb, Mapping):
        return None

    cmdb_org = _text(cmdb.get("org_id"))
    effective_org = _text(org_id)
    if effective_org and cmdb_org and cmdb_org != effective_org:
        return None

    items = cmdb.get("configuration_items")
    index: Dict[str, Dict[str, Any]] = {}
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            sys_id = _text(item.get("sys_id"))
            if not sys_id:
                continue
            item_org = _text(item.get("org_id"))
            if effective_org and item_org and item_org != effective_org:
                continue
            index[sys_id] = {
                "ci_class": _text(item.get("ci_class") or item.get("sys_class_name")),
                "ci_name": _text(item.get("name")),
                "updated_at": _text(item.get("updated_at") or item.get("sys_updated_on")),
                "source_url": _text(item.get("source_url")),
            }
    return index


def extract_event_signatures(*sources: Any) -> Tuple[str, ...]:
    """Collect explicit, well-formed event signatures from incident-shaped sources.

    Reads only the explicit link fields (:data:`EVENT_SIGNATURE_LIST_FIELDS` /
    :data:`EVENT_SIGNATURE_SCALAR_FIELDS`) and accepts a value ONLY when it
    matches the deterministic ``event_signature`` shape — never free text. The
    result is de-duplicated and sorted for determinism.
    """
    found = set()
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for field in EVENT_SIGNATURE_LIST_FIELDS:
            for entry in _signature_candidates(source.get(field)):
                text = _text(entry)
                if text and _EVENT_SIGNATURE_RE.match(text):
                    found.add(text)
        for field in EVENT_SIGNATURE_SCALAR_FIELDS:
            text = _text(source.get(field))
            if text and _EVENT_SIGNATURE_RE.match(text):
                found.add(text)
    return tuple(sorted(found))


def has_event_signature_field(*sources: Any) -> bool:
    """True when a source CARRIES an event-signature link field at all.

    Distinguishes "the upstream loop was never established" (the field is absent)
    from "a link was supplied but nothing in it parsed" (the field is present and
    yielded no signature). Both leave the join unlinked — the verdict is
    deliberately unchanged — but only the second is a data problem worth chasing,
    and reporting one reason for both makes that undiagnosable.
    """
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for field in EVENT_SIGNATURE_LIST_FIELDS + EVENT_SIGNATURE_SCALAR_FIELDS:
            if source.get(field) is not None:
                return True
    return False


def _signature_candidates(raw: Any) -> Tuple[Any, ...]:
    """Every individual signature carried by one column value.

    The list branch cannot simply test ``isinstance(raw, (list, tuple))``,
    because the incident payload copies this column VERBATIM and ServiceNow is
    queried with ``sysparm_display_value=all`` — so a live multi-value field
    arrives wrapped as ``{"value": ..., "display_value": ...}``, a Mapping, and
    was skipped entirely. Offline fixtures store plain scalars, so every test
    passed while every live multi-value signature was silently dropped.

    Three shapes are therefore handled: a plain list, the ``{value: [...]}``
    wrapper, and the comma-separated string ServiceNow uses for a multi-value
    field inside that wrapper. Splitting on commas is safe because a signature is
    ``"{version}:{hex}"`` and contains no comma — and every candidate is still
    validated against ``_EVENT_SIGNATURE_RE`` by the caller, so a malformed
    fragment is dropped rather than trusted.
    """
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        # Prefer the raw value: display_value renders a reference list for humans,
        # while value carries the canonical stored form.
        inner = raw.get("value")
        if inner is None:
            inner = raw.get("display_value")
        return _signature_candidates(inner)
    if isinstance(raw, (list, tuple)):
        out: list[Any] = []
        for entry in raw:
            out.extend(_signature_candidates(entry))
        return tuple(out)
    if isinstance(raw, str):
        return tuple(part for part in (p.strip() for p in raw.split(",")) if part)
    return (raw,)


# ── the CI-location join ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class CILocationJoin:
    """The B3 CI-location soft join for one recurrence — and its trace fragment.

    ``located`` is the verdict; ``status`` says why (``joined`` /
    ``not_available`` / ``join_failed``) and ``reason`` carries the specific
    cause. ``configuration_items`` lists each distinct located CI with the count
    of the recurrence's incidents tied to it and a resolvable CI evidence pointer.
    """

    status: str
    located: bool
    reason: str
    ci_class: Optional[str]
    ci_id: Optional[str]
    ci_name: Optional[str]
    located_incident_count: int
    member_count: int
    configuration_items: Tuple[Dict[str, Any], ...]

    def to_trace(self) -> Dict[str, Any]:
        return {
            "hop": HOP_INCIDENT_TO_CI,
            "status": self.status,
            "located": self.located,
            "reason": self.reason,
            "ci_class": self.ci_class,
            "ci_id": self.ci_id,
            "ci_name": self.ci_name,
            "located_incident_count": self.located_incident_count,
            "member_count": self.member_count,
            "configuration_items": [dict(ci) for ci in self.configuration_items],
        }


def _member_ci_reference(member: Any) -> Optional[str]:
    """The member's CI sys_id — primary ``cmdb_ci`` first, then affected-CI."""
    ci_id = getattr(member, "ci_reference", None)
    if ci_id:
        return ci_id
    affected = getattr(member, "affected_ci_ids", None) or ()
    for candidate in affected:
        text = _text(candidate)
        if text:
            return text
    return None


def build_ci_location_join(
    members: Sequence[Any],
    cmdb_index: Optional[Mapping[str, Mapping[str, Any]]],
) -> CILocationJoin:
    """Locate a recurrence against B3 CMDB CIs — soft, deterministic, org-scoped.

    * No CMDB block for the run (``cmdb_index is None``) → ``not_available``
      (``b3_cmdb_absent``): the B3 soft dependency is simply not present (AC5).
    * CMDB present but no incident in the recurrence names a CI →
      ``not_available`` (``no_ci_reference``): nothing to locate against.
    * CMDB present, incidents name CIs, but none resolve → ``join_failed``
      (``unresolved_ci_reference``): the join was attempted and did not land.
    * Otherwise → ``joined`` with the located CI class/id/name and provenance.
    """
    member_count = len(members)
    refs: List[Tuple[Any, str]] = []
    for member in members:
        ci_id = _member_ci_reference(member)
        if ci_id:
            refs.append((member, ci_id))

    if cmdb_index is None:
        return CILocationJoin(
            status=STATUS_NOT_AVAILABLE,
            located=False,
            reason="b3_cmdb_absent",
            ci_class=None,
            ci_id=None,
            ci_name=None,
            located_incident_count=0,
            member_count=member_count,
            configuration_items=(),
        )
    if not refs:
        return CILocationJoin(
            status=STATUS_NOT_AVAILABLE,
            located=False,
            reason="no_ci_reference",
            ci_class=None,
            ci_id=None,
            ci_name=None,
            located_incident_count=0,
            member_count=member_count,
            configuration_items=(),
        )

    resolved: Dict[str, Dict[str, Any]] = {}
    for member, ci_id in refs:
        ci = cmdb_index.get(ci_id)
        if not ci or not ci.get("ci_class"):
            continue
        bucket = resolved.setdefault(
            ci_id,
            {
                "ci_id": ci_id,
                "ci_class": ci.get("ci_class"),
                "ci_name": ci.get("ci_name"),
                "incident_count": 0,
                "updated_at": ci.get("updated_at"),
                "source_url": ci.get("source_url"),
            },
        )
        bucket["incident_count"] += 1

    if not resolved:
        return CILocationJoin(
            status=STATUS_JOIN_FAILED,
            located=False,
            reason="unresolved_ci_reference",
            ci_class=None,
            ci_id=None,
            ci_name=None,
            located_incident_count=0,
            member_count=member_count,
            configuration_items=(),
        )

    configuration_items: List[Dict[str, Any]] = []
    for ci_id in sorted(resolved):
        bucket = resolved[ci_id]
        pointer = EvidencePointer.observed(
            source_system="servicenow",
            source_artifact=ci_id,
            source_timestamp=bucket.get("updated_at"),
            source_artifact_type="record_id",
        ).to_dict()
        configuration_items.append(
            {
                "ci_id": ci_id,
                "ci_class": bucket["ci_class"],
                "ci_name": bucket["ci_name"],
                "incident_count": bucket["incident_count"],
                "source_url": bucket["source_url"],
                "evidence": pointer,
            }
        )

    located_incident_count = sum(ci["incident_count"] for ci in configuration_items)
    distinct_classes = {ci["ci_class"] for ci in configuration_items}
    common_class = next(iter(distinct_classes)) if len(distinct_classes) == 1 else None
    single = configuration_items[0] if len(configuration_items) == 1 else None

    return CILocationJoin(
        status=STATUS_JOINED,
        located=True,
        reason="located",
        ci_class=common_class,
        ci_id=single["ci_id"] if single else None,
        ci_name=single["ci_name"] if single else None,
        located_incident_count=located_incident_count,
        member_count=member_count,
        configuration_items=tuple(configuration_items),
    )


# ── the event-signature join ─────────────────────────────────────────────────

@dataclass(frozen=True)
class EventSignatureJoin:
    """The B0/B7 event-signature soft join for one recurrence — and its trace.

    Records ONLY explicit, well-formed event signatures the upstream bridge has
    already tied to the recurrence's incidents. ``linked`` is the verdict; when no
    incident carries an explicit link the status is ``not_available`` — the
    upstream loop was never established, which is not a failure (AC /
    conservative). ``reason`` separates the two ways that happens:
    ``no_event_link`` (no incident carried a link field) and
    ``event_link_parse_failed`` (one did, and nothing in it parsed as a
    deterministic signature). Each entry in ``event_links`` names one
    signature and the incidents it links, completing the
    alert→incident→resolution loop with the recurrence's resolution evidence.
    """

    status: str
    linked: bool
    reason: str
    event_signatures: Tuple[str, ...]
    linked_incident_count: int
    member_count: int
    event_links: Tuple[Dict[str, Any], ...]

    def to_trace(self) -> Dict[str, Any]:
        return {
            "hop": HOP_EVENT_TO_INCIDENT_TO_RESOLUTION,
            "status": self.status,
            "linked": self.linked,
            "reason": self.reason,
            "event_signatures": list(self.event_signatures),
            "linked_incident_count": self.linked_incident_count,
            "member_count": self.member_count,
            "event_links": [dict(link) for link in self.event_links],
        }


def build_event_signature_join(members: Sequence[Any]) -> EventSignatureJoin:
    """Link a recurrence to operational event signatures — explicit links only.

    Reads each member's explicit ``event_signatures`` (populated upstream by the
    B0/B7 event bridge). No signature on any incident → ``not_available``, with
    the reason naming whether a link field was absent (``no_event_link``) or
    present-but-unparseable (``event_link_parse_failed``). Never derives a link
    from timing or text.
    """
    member_count = len(members)
    linked: Dict[str, List[Any]] = {}
    linked_members = set()
    for member in members:
        signatures = getattr(member, "event_signatures", None) or ()
        for signature in signatures:
            linked.setdefault(signature, []).append(member)
            linked_members.add(id(member))

    if not linked:
        # Same verdict either way — the join is soft and an unlinked recurrence is
        # still emitted — but say WHICH of the two happened: no incident carried a
        # link field at all, or one did and nothing in it parsed as a deterministic
        # signature. Reporting "no_event_link" for both makes a misconfigured or
        # malformed upstream link indistinguishable from an absent one.
        attempted = any(
            getattr(member, "event_signature_field_present", False)
            for member in members
        )
        return EventSignatureJoin(
            status=STATUS_NOT_AVAILABLE,
            linked=False,
            reason="event_link_parse_failed" if attempted else "no_event_link",
            event_signatures=(),
            linked_incident_count=0,
            member_count=member_count,
            event_links=(),
        )

    event_links: List[Dict[str, Any]] = []
    for signature in sorted(linked):
        signature_members = sorted(
            linked[signature],
            key=lambda m: (
                getattr(m, "incident_sys_id", "") or "",
                getattr(m, "incident_number", "") or "",
            ),
        )
        examples = []
        for member in signature_members:
            pointer = _safe_pointer(getattr(member, "evidence", None))
            examples.append(
                {
                    "incident_sys_id": getattr(member, "incident_sys_id", None),
                    "incident_number": getattr(member, "incident_number", None),
                    "resolution_signature": getattr(member, "resolution_signature", None),
                    "evidence": pointer,
                }
            )
        event_links.append(
            {
                "event_signature": signature,
                "incident_count": len(signature_members),
                "incidents": examples,
            }
        )

    return EventSignatureJoin(
        status=STATUS_JOINED,
        linked=True,
        reason="linked",
        event_signatures=tuple(sorted(linked)),
        linked_incident_count=len(linked_members),
        member_count=member_count,
        event_links=tuple(event_links),
    )


# ── the assembled evidence trace ─────────────────────────────────────────────

def build_evidence_trace(
    members: Sequence[Any],
    ci_join: CILocationJoin,
    event_join: EventSignatureJoin,
    example_evidence_pointers: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Assemble the recurrence's hop-by-hop evidence trace.

    The first hop (recurrence → incident) is ALWAYS present — it is the
    recurrence's own resolvable examples. The CI and event hops carry their soft
    status so a consumer can see exactly which hops landed and which did not, and
    why (``not_available`` vs ``join_failed``).
    """
    recurrence_hop = {
        "hop": HOP_RECURRENCE_TO_INCIDENT,
        "status": "present",
        "incident_count": len(members),
        "example_evidence_pointers": [dict(pointer) for pointer in example_evidence_pointers],
    }
    return {
        "hops": [recurrence_hop, ci_join.to_trace(), event_join.to_trace()],
        "located": ci_join.located,
        "event_linked": event_join.linked,
    }
