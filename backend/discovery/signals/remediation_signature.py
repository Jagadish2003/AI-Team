"""MSP-B11 T4 - deterministic vulnerability-remediation signatures.

This rule follows the versioned, ordered-component hashing contract in
``event_signature.py`` and the structured-field discipline in
``resolution_signature.py``.  It identifies recurring remediation WORKFLOW,
not affected hosts or individual vulnerabilities.

Included components, in this exact order
----------------------------------------
1. ``vulnerability_class`` - the bounded class from Vulnerability Response.
2. ``ci_class`` - the resolved MSP-B3 Configuration Item class.
3. ``remediation_path`` - the ordered lifecycle-state path derived from
   ``state_history`` and the current state.

Every token is trimmed, internal whitespace is collapsed, and text is
casefolded.  Consecutive duplicate path states are removed.  Audited state
transitions are sorted by their source timestamp before the path is assembled,
so the same source history produces the same path regardless of response order.

Deliberately excluded
---------------------
Vulnerable-item IDs/numbers, CI IDs, host/display names, vulnerability or CVE
IDs, timestamps, scan/run IDs, severity, assignees, assignment groups, source
URLs, free text, and scanner payloads never participate.  Consequently the
workload aggregation can count recurring class-level remediation patterns but
cannot enumerate host-by-vulnerability pairs.

The signature is ``"{VERSION}:{sha256_128bit_hex}"`` over the three canonical
components joined by the same ASCII unit separator used by the other
operational signatures.  Bump :data:`REMEDIATION_SIGNATURE_VERSION` whenever
the recipe or normalization rules change.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from .resolution_signature import normalize_token


REMEDIATION_SIGNATURE_VERSION = "1"
REMEDIATION_SIGNATURE_INCLUDED_FIELDS: Tuple[str, ...] = (
    "vulnerability_class",
    "ci_class",
    "remediation_path",
)
REMEDIATION_SIGNATURE_EXCLUDED_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "number",
    "cmdb_ci",
    "ci_sys_id",
    "host_name",
    "display_name",
    "vulnerability",
    "vulnerability_id",
    "cve",
    "cve_id",
    "source_timestamp",
    "first_found",
    "last_found",
    "opened_at",
    "created_at",
    "resolved_at",
    "closed_at",
    "scan_run_id",
    "run_id",
    "severity",
    "assigned_to",
    "assignment_group",
    "source_url",
)

_SEP = "\x1f"
_PATH_SEP = "\x1e"
_PATH_ARROW_RE = re.compile(r"\s*(?:->|=>|\u2192)\s*")


def _collapse_path(states: Iterable[Any]) -> Tuple[str, ...]:
    path = []
    for state in states:
        token = normalize_token(state)
        if token and (not path or path[-1] != token):
            path.append(token)
    return tuple(path)


def normalize_remediation_path(path: Any) -> Tuple[str, ...]:
    """Normalize an already-ordered remediation path.

    A path may be a sequence of state values or an arrow-delimited string.
    State order remains meaningful; only case/whitespace and consecutive
    duplicates are normalized.
    """
    if path is None:
        return ()
    if isinstance(path, str):
        parts = _PATH_ARROW_RE.split(path.strip()) if path.strip() else []
        return _collapse_path(parts)
    if isinstance(path, Mapping):
        nested = path.get("states") or path.get("path")
        if nested is not None:
            return normalize_remediation_path(nested)
        return _collapse_path((path.get("from_value"), path.get("to_value")))
    if isinstance(path, Sequence):
        return _collapse_path(path)
    return _collapse_path((path,))


def remediation_path_from_history(
    state_history: Any,
    *,
    current_state: Any = None,
) -> Tuple[str, ...]:
    """Build the ordered state path from ServiceNow audit transitions.

    Mapping-shaped transitions are sorted by ``changed_at`` with normalized
    transition values as stable tie-breakers.  If callers pass a plain sequence
    of states, that explicit state order is preserved.
    """
    if not state_history:
        return _collapse_path((current_state,))
    if isinstance(state_history, str) or not isinstance(state_history, Sequence):
        path = list(normalize_remediation_path(state_history))
        return _collapse_path((*path, current_state))

    transitions = []
    direct_states = []
    for position, entry in enumerate(state_history):
        if not isinstance(entry, Mapping):
            direct_states.append(entry)
            continue
        field = normalize_token(entry.get("field"))
        if field and field != "state":
            continue
        transitions.append(
            (
                normalize_token(entry.get("changed_at")),
                normalize_token(entry.get("from_value")),
                normalize_token(entry.get("to_value")),
                position,
            )
        )

    if transitions and not direct_states:
        # Timestamped audit rows may arrive in any response order.  When every
        # timestamp is absent, preserve the connector's existing source order.
        if any(changed_at for changed_at, _, _, _ in transitions):
            transitions.sort(
                key=lambda transition: (
                    not bool(transition[0]),
                    transition[0],
                    transition[1],
                    transition[2],
                )
            )
        states = []
        for _, from_value, to_value, _ in transitions:
            states.extend((from_value, to_value))
        states.append(current_state)
        return _collapse_path(states)

    return _collapse_path((*direct_states, current_state))


def remediation_signature_components(
    *,
    vulnerability_class: Any,
    ci_class: Any,
    remediation_path: Any,
) -> Dict[str, Any]:
    """Return the exact normalized components behind the signature."""
    return {
        "version": REMEDIATION_SIGNATURE_VERSION,
        "recipe": list(REMEDIATION_SIGNATURE_INCLUDED_FIELDS),
        "vulnerability_class": normalize_token(vulnerability_class),
        "ci_class": normalize_token(ci_class),
        "remediation_path": list(normalize_remediation_path(remediation_path)),
    }


def compute_remediation_signature(
    *,
    vulnerability_class: Any,
    ci_class: Any,
    remediation_path: Any,
) -> str:
    """Return a deterministic fingerprint of one remediation workflow pattern."""
    components = remediation_signature_components(
        vulnerability_class=vulnerability_class,
        ci_class=ci_class,
        remediation_path=remediation_path,
    )
    serialized = [
        components["vulnerability_class"],
        components["ci_class"],
        _PATH_SEP.join(components["remediation_path"]),
    ]
    digest = hashlib.sha256(_SEP.join(serialized).encode("utf-8")).hexdigest()[:32]
    return f"{REMEDIATION_SIGNATURE_VERSION}:{digest}"


def apply_remediation_signature(item: Dict[str, Any]) -> str | None:
    """Annotate one resolved vulnerable-item signal, or explain why it cannot."""
    resolved_ci = item.get("resolved_ci")
    ci_class = resolved_ci.get("ci_class") if isinstance(resolved_ci, Mapping) else None
    vulnerability_class = normalize_token(item.get("vulnerability_class"))
    normalized_ci_class = normalize_token(ci_class)
    path = remediation_path_from_history(
        item.get("state_history"),
        current_state=item.get("state"),
    )

    missing_reason = None
    if not vulnerability_class:
        missing_reason = "missing_vulnerability_class"
    elif not normalized_ci_class:
        missing_reason = "missing_resolved_ci_class"
    elif not path:
        missing_reason = "missing_remediation_path"
    if missing_reason:
        item["remediation_signature"] = None
        item["remediation_signature_components"] = None
        item["remediation_signature_reason"] = missing_reason
        return None

    signature = compute_remediation_signature(
        vulnerability_class=vulnerability_class,
        ci_class=normalized_ci_class,
        remediation_path=path,
    )
    item["remediation_signature"] = signature
    item["remediation_signature_components"] = remediation_signature_components(
        vulnerability_class=vulnerability_class,
        ci_class=normalized_ci_class,
        remediation_path=path,
    )
    item.pop("remediation_signature_reason", None)
    return signature


def aggregate_remediation_workload(
    vulnerable_items: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate class-level remediation effort without host/vulnerability pairs."""
    items = [item for item in vulnerable_items if isinstance(item, dict)]
    groups: Dict[str, Dict[str, Any]] = {}
    for item in items:
        signature = item.get("remediation_signature")
        components = item.get("remediation_signature_components")
        if not signature or not isinstance(components, Mapping):
            continue
        group = groups.setdefault(
            str(signature),
            {
                "remediation_signature": str(signature),
                "vulnerability_class": components.get("vulnerability_class"),
                "ci_class": components.get("ci_class"),
                "remediation_path": list(components.get("remediation_path") or []),
                "item_count": 0,
            },
        )
        group["item_count"] += 1

    patterns = sorted(
        groups.values(),
        key=lambda group: (-group["item_count"], group["remediation_signature"]),
    )
    signed_items = sum(group["item_count"] for group in patterns)
    return {
        "total_items": len(items),
        "signed_items": signed_items,
        "unsigned_items": len(items) - signed_items,
        "pattern_count": len(patterns),
        "patterns": patterns,
    }


def apply_remediation_signatures(
    vulnerable_items: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Annotate every item and return its deterministic workload aggregation."""
    items = [item for item in vulnerable_items if isinstance(item, dict)]
    for item in items:
        apply_remediation_signature(item)
    return aggregate_remediation_workload(items)
