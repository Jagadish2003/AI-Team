"""Four-part finding contract for authored packs — 2.0-C3 T2 (AT-837).

The ticket's requirement in one sentence: *evidence/corroboration semantics built
in, so the four-part criterion is inherited rather than re-implemented*. This
module is where "inherited" is made literal.

Inherited, not re-copied
------------------------
The four-part vocabulary and its builders come from ``cloud_ops_finding`` — the
operational pack scaffold — imported unchanged, exactly as ``security_ops_finding``
inherits them (MSP-B12's precedent). A partner finding is therefore the SAME
object shape as a first-party one: the same ``evidence`` / ``confidence`` /
``corroboration`` / ``source_trace`` parts, the same "no individuals" sweep, the
same causal gate. This module is the SDK's import site for the contract builders,
so if that scaffold is ever renamed, this file changes. (``signals.py`` reads the
same scaffold directly for the admission-time individual denylist — it cannot
import this module without a cycle. Two importers of one source, never a copy.)

What this module adds on top is the part an author must not be allowed to write:
the DERIVATION of confidence and corroboration from the contributing records. A
pack cannot assert "HIGH" — it declares a detector, and the level follows from how
many independent sources agree, with the standing ceilings applied:

* one source            → MEDIUM, capped, labelled single-source;
* two or more sources   → corroborated, HIGH-eligible;
* conversation-derived  → never above MEDIUM on its own, however many chat
  sources agree (the standing R16/1.9 ceiling);
* a manifest's own caps → applied on top, and they may only LOWER (the schema
  refuses raising them, AT-836).

A join-based finding additionally records the join type and the correlation window
that produced it, on success — the MSP-B7 discipline, so a coincidence outside the
window can never read as agreement.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..cloud_ops_finding import (  # noqa: F401 - re-exported as the SDK's one seam
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    FOUR_PART_CONTRACT_FIELDS,
    INDIVIDUAL_FIELD_DENYLIST,
    SINGLE_SOURCE_LABEL,
    STATUS_CORROBORATED,
    STATUS_SINGLE_SOURCE,
    assert_no_individual_references,
    assert_not_causal,
    build_concentration_statement,
    build_confidence,
    build_corroboration,
    build_finding_contract,
    build_source_trace,
    find_causal_language,
    find_individual_references,
    is_contract_complete,
    missing_contract_parts,
)
from .signals import CONVERSATION_SOURCE_SYSTEMS

_LEVEL_RANK = {CONFIDENCE_LOW: 0, CONFIDENCE_MEDIUM: 1, CONFIDENCE_HIGH: 2}

#: Manifest cap keys (validated by the schema; re-stated here as the read side).
CAP_SINGLE_SOURCE = "singleSourceCap"
CAP_CORROBORATED_MAX = "corroboratedMax"
CAP_CONVERSATION = "conversationSourceCap"

CONVERSATION_CAP_REASON = (
    "Conversation-derived corroboration is capped at MEDIUM — chat evidence "
    "supports a finding, it never establishes it."
)


class PackContractViolation(ValueError):
    """An authored pack's finding failed the four-part contract at the boundary.

    Raised to FAIL the pack's execution, never swallowed — the same posture as
    ``CloudOpsContractViolation``. A partner pack is held to the identical bar as
    a first-party one; that is the whole point of certification meaning something.
    """


def _lower_of(level: str, ceiling: Optional[str]) -> str:
    if not ceiling:
        return level
    ceiling = str(ceiling).upper()
    if ceiling not in _LEVEL_RANK:
        return level
    return level if _LEVEL_RANK[level] <= _LEVEL_RANK[ceiling] else ceiling


def derive_confidence(
    source_systems: Sequence[str],
    *,
    caps: Optional[Mapping[str, str]] = None,
    window_gated: bool = False,
    join_type: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Derive the confidence AND corroboration parts from the contributing sources.

    The author supplies no level anywhere — this is the whole mechanism by which a
    partner pack inherits honest confidence instead of asserting it.

    Returns ``{"confidence": {...}, "corroboration": {...}}``.
    """
    caps = dict(caps or {})
    distinct: List[str] = []
    for system in source_systems:
        name = str(system or "").strip()
        if name and name not in distinct:
            distinct.append(name)

    substantive = [
        system for system in distinct if system not in CONVERSATION_SOURCE_SYSTEMS
    ]
    conversation_involved = len(substantive) < len(distinct)

    if len(distinct) < 2:
        level = _lower_of(CONFIDENCE_MEDIUM, caps.get(CAP_SINGLE_SOURCE))
        if conversation_involved:
            level = _lower_of(level, caps.get(CAP_CONVERSATION))
        confidence = build_confidence(
            level,
            capped=True,
            eligible_for_high=False,
            cap_reason=(
                CONVERSATION_CAP_REASON
                if conversation_involved
                else "Single-source observation — no independent system agrees yet."
            ),
        )
        corroboration = build_corroboration(
            STATUS_SINGLE_SOURCE,
            sources=distinct,
            label=SINGLE_SOURCE_LABEL,
            window_gated=window_gated,
        )
        return {"confidence": confidence, "corroboration": corroboration}

    # Two or more independent sources agree. HIGH is reachable only when at least
    # two of them are non-conversational.
    if len(substantive) >= 2:
        level = _lower_of(CONFIDENCE_HIGH, caps.get(CAP_CORROBORATED_MAX))
        capped = level != CONFIDENCE_HIGH
        cap_reason = (
            "Pack calibration caps corroborated findings below HIGH." if capped else ""
        )
    else:
        level = _lower_of(CONFIDENCE_MEDIUM, caps.get(CAP_CONVERSATION))
        capped = True
        cap_reason = CONVERSATION_CAP_REASON

    label = f"Corroborated across {', '.join(distinct)}"
    if window_gated and join_type:
        label += f" ({join_type} agreement inside the correlation window)"

    return {
        "confidence": build_confidence(
            level,
            capped=capped,
            eligible_for_high=not capped,
            cap_reason=cap_reason,
        ),
        "corroboration": build_corroboration(
            STATUS_CORROBORATED,
            sources=distinct,
            label=label,
            window_gated=window_gated,
        ),
    }


def build_pack_contract(
    *,
    evidence: Mapping[str, Any],
    source_systems: Sequence[str],
    artifacts: Sequence[Mapping[str, Any]],
    caps: Optional[Mapping[str, str]] = None,
    window_gated: bool = False,
    join_type: str = "",
    statement: str = "",
) -> Dict[str, Any]:
    """Assemble a complete four-part contract for an authored-pack finding.

    Confidence and corroboration are DERIVED (see :func:`derive_confidence`); the
    caller supplies only observed facts and pointers. The statement, when present,
    passes the causal gate before it can reach a finding — a partner pack cannot
    ship "caused by" wording any more than a first-party one can.
    """
    if statement:
        assert_not_causal(statement)
    derived = derive_confidence(
        source_systems, caps=caps, window_gated=window_gated, join_type=join_type
    )
    payload = dict(evidence)
    if statement:
        payload["statement"] = statement
    return build_finding_contract(
        evidence=payload,
        confidence=derived["confidence"],
        corroboration=derived["corroboration"],
        source_trace=build_source_trace(
            systems=list(dict.fromkeys(source_systems)),
            artifacts=[dict(artifact) for artifact in artifacts],
        ),
    )


def enforce_pack_contract(
    contract: Any, *, detector_id: str = "", index: Optional[int] = None
) -> None:
    """Raise :class:`PackContractViolation` unless the contract is complete and
    individual-free. No-op when valid."""
    where = f"detector {detector_id!r}" if detector_id else "finding"
    if index is not None:
        where += f" (index {index})"
    if contract is None:
        raise PackContractViolation(
            f"{where} carries no four-part finding_contract — every finding must "
            f"carry {list(FOUR_PART_CONTRACT_FIELDS)}."
        )
    missing = missing_contract_parts(contract)
    if missing:
        raise PackContractViolation(
            f"{where} is missing required contract part(s) {missing}; a finding must "
            f"carry all four: {list(FOUR_PART_CONTRACT_FIELDS)}."
        )
    leaked = find_individual_references(contract)
    if leaked:
        raise PackContractViolation(
            f"{where} references an individual (forbidden — groups, queues, "
            f"services, and entities only): {leaked}."
        )


__all__ = [
    "CAP_CONVERSATION",
    "CAP_CORROBORATED_MAX",
    "CAP_SINGLE_SOURCE",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "FOUR_PART_CONTRACT_FIELDS",
    "INDIVIDUAL_FIELD_DENYLIST",
    "PackContractViolation",
    "SINGLE_SOURCE_LABEL",
    "STATUS_CORROBORATED",
    "STATUS_SINGLE_SOURCE",
    "assert_no_individual_references",
    "assert_not_causal",
    "build_concentration_statement",
    "build_pack_contract",
    "derive_confidence",
    "enforce_pack_contract",
    "find_causal_language",
    "find_individual_references",
    "is_contract_complete",
    "missing_contract_parts",
]
