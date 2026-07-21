"""
cloud_ops_finding.py — MSP-B6 T2 (AT-737): the four-part finding contract for the
Cloud-Operations Discovery Pack.

Every finding this pack emits must carry all four parts (MSP-B6 §"four-part
criterion" / T2 AC3):

  1. evidence            — the observed numbers/facts behind the finding.
  2. confidence          — a level (LOW/MEDIUM/HIGH) plus whether it is capped and
                           why; single-source findings are capped, corroborated
                           ones are HIGH-eligible.
  3. corroboration       — which independent sources agree, or an explicit
                           "single-source, confidence capped accordingly".
  4. source_trace        — the originating systems and artifacts (incident ids,
                           event signatures, queue names, services/CIs).

This module builds those parts consistently so the four detectors (T2) and the
shared-CI hotspot (T3) speak one shape, and so the pack-boundary enforcement (T6)
has a single definition to validate against. It contains NO detector or scorer
logic — only contract construction and the "no individuals" guarantee helpers.

The pack surfaces GROUPS, QUEUES, SERVICES, and CIs only — never an individual
person (MSP-B6 AC2/AC7). Detectors achieve this by reading only group/queue/
service fields; ``find_individual_references`` makes the guarantee testable and is
the sweep T6/tests run over an emitted finding.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

try:  # shared confidence vocabulary — keep in lockstep with the corroboration engine
    from backend.discovery.packs.corroboration_rules import (
        CONFIDENCE_HIGH,
        CONFIDENCE_LOW,
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
    )
except ModuleNotFoundError:  # pragma: no cover - import shim
    from discovery.packs.corroboration_rules import (
        CONFIDENCE_HIGH,
        CONFIDENCE_LOW,
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
    )

# The four contract parts, in order. T6 enforces that every finding carries them.
FOUR_PART_CONTRACT_FIELDS = ("evidence", "confidence", "corroboration", "source_trace")

# Corroboration status vocabulary.
STATUS_CORROBORATED = "corroborated"
STATUS_SINGLE_SOURCE = "single_source"

# The explicit single-source label (honest confidence — MSP-B6 §"Honest confidence").
SINGLE_SOURCE_LABEL = "Single-source, confidence capped accordingly"

# Field names that would name or point to an INDIVIDUAL person. The pack must
# never surface any of these — findings speak groups/queues/services/CIs only.
# Detectors read only safe fields; this denylist + the sweep below make the
# guarantee enforceable (MSP-B6 AC2/AC7).
INDIVIDUAL_FIELD_DENYLIST = frozenset({
    "assignee",
    "assigned_to",
    "assigned_to_user",
    "assigned_user",
    "user",
    "user_id",
    "username",
    "user_name",
    "person",
    "caller",
    "caller_id",
    "opened_by",
    "closed_by",
    "resolved_by",
    "updated_by",
    "owner_user",
    "individual",
    "email",
    "user_email",
    "full_name",
    "display_name",
    "first_name",
    "last_name",
})

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


# ── Contract-part builders ──────────────────────────────────────────────────────


def build_confidence(
    level: str,
    *,
    capped: bool,
    eligible_for_high: bool,
    cap_reason: str = "",
    note: str = "",
) -> Dict[str, Any]:
    """Build the confidence part.

    level             — the finding's confidence (LOW/MEDIUM/HIGH).
    capped            — True when the level is being held down (e.g. single-source).
    eligible_for_high — True when corroboration makes HIGH reachable.
    cap_reason        — why it is capped (shown honestly on the finding).
    """
    level = str(level).upper()
    if level not in CONFIDENCE_ORDER:
        raise ValueError(
            f"confidence level must be one of {sorted(CONFIDENCE_ORDER)}, got {level!r}"
        )
    return {
        "level": level,
        "capped": bool(capped),
        "eligible_for_high": bool(eligible_for_high),
        "cap_reason": cap_reason,
        "note": note,
    }


def build_corroboration(
    status: str,
    *,
    sources: Sequence[str],
    label: str,
    window_gated: bool = False,
    rule_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build the corroboration part.

    status       — STATUS_CORROBORATED or STATUS_SINGLE_SOURCE.
    sources      — the independent systems that agree (e.g. ["servicenow", "events"]).
    label        — the human on-card label ("Corroborated by ..." / single-source).
    window_gated — True when the corroborating join is time-window gated (B7).
    """
    if status not in (STATUS_CORROBORATED, STATUS_SINGLE_SOURCE):
        raise ValueError(
            f"corroboration status must be {STATUS_CORROBORATED!r} or "
            f"{STATUS_SINGLE_SOURCE!r}, got {status!r}"
        )
    return {
        "status": status,
        "sources": list(sources),
        "label": label,
        "window_gated": bool(window_gated),
        "rule_ids": list(rule_ids or []),
    }


def build_source_trace(
    *,
    systems: Sequence[str],
    artifacts: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the source-trace part.

    systems   — originating systems the finding resolves to (e.g. ["servicenow"]).
    artifacts — list of {type, id, ...} pointers to concrete source records
                (incident ids, event signatures, queues, services/CIs).
    """
    return {
        "systems": list(systems),
        "artifacts": [dict(a) for a in artifacts],
    }


def build_finding_contract(
    *,
    evidence: Dict[str, Any],
    confidence: Dict[str, Any],
    corroboration: Dict[str, Any],
    source_trace: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the four-part contract and fail loudly if a part is missing/empty.

    ``evidence`` must contain at least one numeric value (both because it is the
    factual spine of the finding and because DetectorResult.raw_evidence requires
    a number). ``source_trace`` must resolve to at least one system and artifact.
    """
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("finding contract 'evidence' must be a non-empty dict")
    if not any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in _flatten(evidence)):
        raise ValueError("finding contract 'evidence' must contain a numeric value")
    for name, part in (("confidence", confidence), ("corroboration", corroboration)):
        if not isinstance(part, dict) or not part:
            raise ValueError(f"finding contract {name!r} must be a non-empty dict")
    if not source_trace.get("systems") or not source_trace.get("artifacts"):
        raise ValueError("finding contract 'source_trace' must resolve to a system and artifact")

    contract = {
        "evidence": evidence,
        "confidence": confidence,
        "corroboration": corroboration,
        "source_trace": source_trace,
    }
    # Defence in depth: a finding must never carry an individual reference.
    leaked = find_individual_references(contract)
    if leaked:
        raise ValueError(
            f"finding contract references an individual (forbidden — groups/queues/"
            f"services/CIs only): {leaked}"
        )
    return contract


def is_contract_complete(contract: Any) -> bool:
    """Return True when ``contract`` carries all four non-empty parts (T6 helper)."""
    if not isinstance(contract, dict):
        return False
    return all(bool(contract.get(field)) for field in FOUR_PART_CONTRACT_FIELDS)


# ── "No individuals" guarantee (MSP-B6 AC2 / AC7) ───────────────────────────────


def find_individual_references(obj: Any, *, _path: str = "") -> List[str]:
    """Recursively scan a finding/contract for any individual-person reference.

    Flags a denylisted key (``assignee``, ``caller``, ...) that carries a value,
    and any string value that looks like an email address. Returns a list of
    dotted paths to each offending location (empty when clean).
    """
    hits: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{_path}.{key}" if _path else str(key)
            if str(key).lower() in INDIVIDUAL_FIELD_DENYLIST and value not in (None, "", [], {}):
                hits.append(here)
            hits.extend(find_individual_references(value, _path=here))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            hits.extend(find_individual_references(value, _path=f"{_path}[{i}]"))
    elif isinstance(obj, str):
        if _EMAIL_RE.search(obj):
            hits.append(f"{_path} (email-like value)")
    return hits


def assert_no_individual_references(obj: Any) -> None:
    """Raise ValueError if ``obj`` references an individual (used by T6/tests)."""
    hits = find_individual_references(obj)
    if hits:
        raise ValueError(f"finding references individuals (forbidden): {hits}")


# ── internal ─────────────────────────────────────────────────────────────────


def _flatten(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten(v)
    else:
        yield obj
