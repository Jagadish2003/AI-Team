"""
security_ops_common.py — MSP-B12 T2 shared helpers for the Security Operations
detectors.

The five SecOps detectors consume ONLY the normalized, org-scoped workflow signal
MSP-B11 produces, which lands on three sibling ``sn_data`` blocks:

  * ``sn_data['secops']``                — SIR workflow signal (``security_incidents``)
  * ``sn_data['vulnerability_response']`` — VR items / groups / remediation tasks
                                            (+ ``workload_summary`` /
                                            ``remediation_workload_summary``)
  * ``sn_data['cmdb']``                  — MSP-B3 CIs + dependency relationships

This module centralises the reading discipline every detector shares so the
"consume B11 only", determinism, org-scoping, group-only, and provenance rules
cannot drift between detectors:

  * ``_text`` extracts a group/queue/class scalar and NEVER a person field.
  * ``observed_pointer`` builds a valid R16-B1 OBSERVED EvidencePointer back to a
    source record (the access-controlled trace the aggregation floor permits).
  * ``group_sequence`` collapses an ``assignment_history`` into an ordered
    group-only sequence — the group-only history processing reused from
    ``ops_pingpong`` (never an assignee).
  * ``severity_band`` normalises a raw ServiceNow severity label to a band token.
  * ``cmdb_adjacency`` / ``ci_class_index`` turn the raw B3 edges into a directed
    depends-on graph + a CI→class lookup for depth-bounded traversal.

Detectors import these; the four-part contract itself lives in
``packs.security_ops_finding`` (inherited from the operational scaffold).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # package-qualified first, bare fallback (mirrors the other detector modules)
    from backend.app.provenance import EvidencePointer
except ModuleNotFoundError:  # pragma: no cover - import shim
    from app.provenance import EvidencePointer

SOURCE_SYSTEM = "servicenow"

# The canonical severity bands, most→least severe. Used for the severity-band
# weighting the T6 scorer applies (config-driven) and for grouping in findings.
SEVERITY_BANDS: Tuple[str, ...] = ("critical", "high", "medium", "low", "informational")

# Bounded number of representative source-record pointers carried on a finding's
# source_trace. Individual records are reachable one-at-a-time through these
# access-controlled pointers (MSP-B12 aggregation floor); the finding never
# enumerates the full set inline.
DEFAULT_MAX_POINTERS = 10


# ── Block accessors ──────────────────────────────────────────────────────────


def secops_block(sn_data: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return the SIR workflow block (``sn_data['secops']``), or an empty dict."""
    block = (sn_data or {}).get("secops")
    return dict(block) if isinstance(block, Mapping) else {}


def vr_block(sn_data: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return the Vulnerability-Response block, or an empty dict."""
    block = (sn_data or {}).get("vulnerability_response")
    return dict(block) if isinstance(block, Mapping) else {}


def cmdb_block(sn_data: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return the CMDB block (MSP-B3 CIs + edges), or an empty dict."""
    block = (sn_data or {}).get("cmdb")
    return dict(block) if isinstance(block, Mapping) else {}


def _records(block: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    """Return the list of record dicts at ``block[key]`` (defensively typed)."""
    value = block.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


# ── Scalar / timestamp helpers ───────────────────────────────────────────────


def _text(value: Any) -> Optional[str]:
    """Return a trimmed group/queue/class scalar, or None.

    Handles ServiceNow reference objects (``display_value``/``value``) and plain
    scalars. Deliberately group-level: callers pass only group/queue/class fields,
    never ``assigned_to`` or another person field.
    """
    if isinstance(value, Mapping):
        value = (
            value.get("display_value")
            or value.get("displayName")
            or value.get("name")
            or value.get("value")
        )
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    return text or None


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse a ServiceNow/ISO datetime to a UTC-aware datetime, or None."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _text(value)
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_dt(value: datetime) -> str:
    """Format a datetime as a stable UTC ISO-8601 string (no microseconds)."""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def record_timestamp(record: Mapping[str, Any]) -> Optional[str]:
    """Best available source timestamp for a record's evidence pointer."""
    for key in ("source_timestamp", "sys_updated_on", "created_at", "opened_at"):
        text = _text(record.get(key))
        if text:
            return text
    return None


def severity_band(value: Any) -> str:
    """Normalise a raw severity label (e.g. ``"3 - Medium"``) to a band token.

    Returns one of :data:`SEVERITY_BANDS` or ``"unclassified"``. Deterministic and
    case-insensitive; ``"info"`` maps to ``"informational"``.
    """
    text = (_text(value) or "").lower()
    if "info" in text:
        return "informational"
    for band in SEVERITY_BANDS:
        if band in text:
            return band
    return "unclassified"


# ── Org scoping ──────────────────────────────────────────────────────────────


def effective_org(block: Mapping[str, Any], org_id: Optional[str]) -> Optional[str]:
    """Resolve the org this run is scoped to (explicit arg wins, else the block)."""
    return _text(org_id) or _text(block.get("org_id"))


def in_org(record: Mapping[str, Any], effective: Optional[str]) -> bool:
    """True when ``record`` belongs to ``effective`` (or no scoping is in force).

    A record whose own ``org_id`` differs from the run org is excluded — B11 stamps
    org_id on every record, so cross-org leakage cannot reach a finding.
    """
    if not effective:
        return True
    record_org = _text(record.get("org_id"))
    return record_org is None or record_org == effective


# ── Provenance ───────────────────────────────────────────────────────────────


def observed_pointer(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a valid OBSERVED EvidencePointer back to one source record, or None.

    ``source_artifact`` is the record's stable sys_id (a workflow record id — never
    a host or CVE), so a finding traces to the real record through an
    access-controlled pointer without enumerating it inline.
    """
    sys_id = _text(record.get("sys_id") or record.get("id"))
    ts = record_timestamp(record)
    if not sys_id or not ts:
        return None
    pointer = EvidencePointer.observed(
        source_system=SOURCE_SYSTEM,
        source_artifact=sys_id,
        source_timestamp=ts,
        source_artifact_type="record_id",
    )
    return pointer.to_dict() if pointer.is_valid() else None


def pointer_artifacts(
    records: Sequence[Mapping[str, Any]],
    *,
    artifact_type: str,
    max_pointers: int = DEFAULT_MAX_POINTERS,
) -> List[Dict[str, Any]]:
    """Return a bounded, deterministic list of source-record pointer artifacts.

    Each artifact is ``{"type", "id", "evidence_pointer"}`` — a valid pointer to a
    real record. Sorted by (source_timestamp, sys_id) and capped so a finding
    surfaces a representative sample, not the full record set (aggregation floor).
    """
    ordered = sorted(
        records,
        key=lambda r: (record_timestamp(r) or "", _text(r.get("sys_id")) or ""),
    )
    artifacts: List[Dict[str, Any]] = []
    for record in ordered:
        pointer = observed_pointer(record)
        if pointer is None:
            continue
        artifacts.append(
            {"type": artifact_type, "id": pointer["source_artifact"], "evidence_pointer": pointer}
        )
        if len(artifacts) >= max_pointers:
            break
    return artifacts


# ── Group-only assignment history (reused from ops_pingpong principles) ──────────


def group_sequence(record: Mapping[str, Any]) -> List[str]:
    """Return the ordered assignment-GROUP sequence for a record.

    Reads only the ``assignment_history`` transitions (``field == assignment_group``)
    — group/queue names only, NEVER an assignee. Consecutive duplicates collapse
    (a re-save that does not change the group is not a hop). Input order is
    authoritative (B11 sorts transitions by changed_at); a raw ``old/new`` shape is
    also accepted.
    """
    history = record.get("assignment_history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        history = []
    sequence: List[str] = []
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        field = str(entry.get("field") or "").strip().lower()
        # Only assignment_group transitions define group hops. A transition whose
        # field is recorded but is not the assignment group is ignored.
        if field and field not in ("assignment_group", "group", "queue"):
            continue
        for value in (entry.get("from_value"), entry.get("to_value")):
            name = _text(value)
            if not name:
                continue
            if sequence and sequence[-1].casefold() == name.casefold():
                continue
            sequence.append(name)
    return sequence


# ── CMDB dependency graph (MSP-B3) ───────────────────────────────────────────

# Relationship types where source depends on target (target is the more underlying
# CI). ``used_by`` is the inverse (target depends on source), so it is reversed.
_DEPENDS_FORWARD = frozenset({"depends_on", "runs_on", "uses", "connects_to", "hosted_on"})
_DEPENDS_REVERSE = frozenset({"used_by", "hosts", "runs"})


def _edges(cmdb: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    rels = cmdb.get("relationships")
    if not isinstance(rels, Sequence) or isinstance(rels, (str, bytes)):
        return []
    return [r for r in rels if isinstance(r, Mapping)]


def cmdb_adjacency(cmdb: Mapping[str, Any]) -> Dict[str, List[str]]:
    """Build a directed depends-on graph from B3 edges: ``adj[X]`` = CIs X depends on.

    Tolerant of the key variants a relationship record may carry
    (``source_ci_id``/``target_ci_id``, ``from_ci_sys_id``/``to_ci_sys_id``,
    ``parent``/``child``, ``from``/``to``). Direction is normalised so an edge
    always points from the dependent CI to the underlying CI; ``used_by`` reverses.
    """
    adj: Dict[str, List[str]] = {}
    for edge in _edges(cmdb):
        src = _text(
            edge.get("source_ci_id") or edge.get("from_ci_sys_id")
            or edge.get("parent") or edge.get("from")
        )
        dst = _text(
            edge.get("target_ci_id") or edge.get("to_ci_sys_id")
            or edge.get("child") or edge.get("to")
        )
        if not src or not dst:
            continue
        rel = str(edge.get("relationship_type") or "depends_on").strip().lower()
        if rel in _DEPENDS_REVERSE:
            src, dst = dst, src
        adj.setdefault(src, [])
        if dst not in adj[src]:
            adj[src].append(dst)
    # Deterministic neighbour ordering.
    for node in adj:
        adj[node].sort()
    return adj


def ci_class_index(cmdb: Mapping[str, Any]) -> Dict[str, str]:
    """Map each CI sys_id → its CI class, from the CMDB configuration items."""
    index: Dict[str, str] = {}
    for ci in _records(cmdb, "configuration_items"):
        sys_id = _text(ci.get("sys_id") or ci.get("id"))
        ci_class = _text(ci.get("ci_class") or ci.get("sys_class_name"))
        if sys_id and ci_class:
            index[sys_id] = ci_class.lower()
    return index
