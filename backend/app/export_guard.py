"""
export_guard.py — Release 2.0-B1 T5 (AC5): the ONE guard every export path runs.

AC5: "Exports contain no unredacted secrets and no host x vulnerability
enumeration (1.9 aggregation floor holds in export)."

Two disciplines already exist in this codebase, but before T5 they were applied
ad hoc — the signed evidence export (T4) held both lines with private helpers of
its own, and no other export path held either. This module is the single, shared,
public implementation so that:

  * a new export surface inherits the guarantee by calling ONE function rather
    than reimplementing two subtleties correctly; and
  * the accompanying conformance test
    (``tests/unit/test_r2_0_b1_t5_export_surface_conformance.py``) can enumerate
    export surfaces and FAIL when a new one does not route through here — so AC5
    cannot silently regress the next time someone adds an export.

The two disciplines, in the order they must run:

  1. **Secret redaction** (``discovery/ingest/secret_redaction``). Non-reversible
     by construction: the scanner substitutes ``[REDACTED:<name>]`` and returns
     only pattern-TYPE names, never the matched value, and keeps no reverse map.
     Retrieval-sourced content is already redacted upstream ("redact before
     index, always"), but detector-built evidence snippets and narrative prose
     are NOT — and an export leaves the deployment.
  2. **The 1.9 SecOps aggregation floor**
     (``discovery/packs/security_ops_aggregation_floor``), whose own docstring
     names exports as a covered surface. It FAILS the export rather than
     sanitising: a signed readout that doubles as a host x vulnerability target
     list is a catastrophic artifact to hand a third party, so it is refused,
     never emitted with a caveat.

Order matters: redaction runs FIRST so that what is swept, hashed, signed, and
written are the same bytes.

Deliberate design decisions, recorded so a later reader does not "fix" them:

  * **Fail-closed, not sanitise-and-serve.** ``assert_export_safe`` raises. This
    is why the guard belongs on EXPORT paths (a document a third party keeps) and
    NOT on the ordinary viewer API reads that back the UI: the floor's IPv4
    pattern matches any valid dotted quad, so a version string like
    "upgraded to 1.2.3.4" in LLM prose would turn a board-facing page into a hard
    error. Exports are the surface where refusing is the correct answer.
  * **Base redaction patterns by default.** ``scan_and_redact`` removes
    credentials (keys/tokens/passwords). The stricter ``scan_and_redact_security``
    additionally scrubs IPs/MACs/emails/hashes — i.e. the very identities the
    floor hard-fails on — so ``strict=True`` converts a refusal into a scrub.
    That is the right trade for a security-note corpus but is LOSSY for ordinary
    content (dotted-quad version strings, contact emails), and it would erase the
    audit trail's actor identity. It is therefore opt-in per surface, and the
    choice each surface makes is stated at its call site.
  * **No sweep cap.** The floor is O(total string bytes) with several tree walks
    and has no internal bound, so the CALLER owns the size bound (the signed
    export caps at ``MAX_REPORT_FINDINGS``). Adding a cap here that skipped part
    of a payload would reintroduce exactly the hole this module closes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ExportGuardViolation",
    "GuardedExport",
    "redact_export_content",
    "assert_export_safe",
    "find_export_violations",
    "guard_export_payload",
]


class ExportGuardViolation(Exception):
    """An export was refused because its content breaches the aggregation floor.

    Raised (never swallowed) so the export fails loudly. Callers map this onto
    their own error type / HTTP status; what they must NOT do is downgrade it to
    a warning and emit the artifact anyway.
    """


@dataclass(frozen=True)
class GuardedExport:
    """The result of guarding one export payload.

    ``payload`` is the redacted content — the bytes the caller must actually
    export, hash, and sign. ``redacted_pattern_types`` is the sorted set of
    pattern-TYPE names that fired (never values), suitable for recording on the
    artifact so a reader can see redaction ran.
    """

    payload: Any
    redacted_pattern_types: List[str] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.redacted_pattern_types)


# ── module resolution (import shims mirror the pack modules' own) ────────────


def _aggregation_floor():
    """The SecOps aggregation-floor module, or None when unavailable."""
    try:  # pragma: no cover - import shim
        from backend.discovery.packs import security_ops_aggregation_floor as floor
    except ModuleNotFoundError:
        try:
            from discovery.packs import security_ops_aggregation_floor as floor
        except ModuleNotFoundError:
            return None
    return floor


def _redactor(strict: bool):
    """The secret-redaction scanner, or None when unavailable."""
    try:  # pragma: no cover - import shim
        from backend.discovery.ingest import secret_redaction as mod
    except ModuleNotFoundError:
        try:
            from discovery.ingest import secret_redaction as mod
        except ModuleNotFoundError:
            return None
    return mod.scan_and_redact_security if strict else mod.scan_and_redact


# ── 1. redaction ────────────────────────────────────────────────────────────


def _redact_tree(value: Any, pattern_types: List[str], scan) -> Any:
    """Recursively redact secret signatures from every string in ``value``.

    Deterministic (pattern-based), so it does not disturb the reproducibility an
    export's signature depends on. Mapping KEYS are left untouched — a key is
    structure, not content, and rewriting one would corrupt the document shape.
    """
    if isinstance(value, str):
        outcome = scan(value)
        if outcome.pattern_types:
            pattern_types.extend(outcome.pattern_types)
        return outcome.text
    if isinstance(value, Mapping):
        return {k: _redact_tree(v, pattern_types, scan) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        redacted = [_redact_tree(v, pattern_types, scan) for v in value]
        return tuple(redacted) if isinstance(value, tuple) else redacted
    return value


def redact_export_content(payload: Any, *, strict: bool = False) -> GuardedExport:
    """Redact secrets from an export payload.

    ``strict=True`` selects the security pattern set (additionally scrubs
    IPs/MACs/emails/hashes) — see the module docstring for when that is right.

    A missing scanner degrades to a no-op that is logged at WARNING rather than
    passing silently: an export that skipped redaction must be visible in the
    logs, not indistinguishable from one that had nothing to redact.
    """
    scan = _redactor(strict)
    if scan is None:
        logger.warning(
            "export_guard: secret-redaction scanner unavailable — exporting "
            "without the redaction pass"
        )
        return GuardedExport(payload=payload, redacted_pattern_types=[])
    pattern_types: List[str] = []
    redacted = _redact_tree(payload, pattern_types, scan)
    if pattern_types:
        # Types + counts only — never a redacted value (that is the whole point).
        logger.info(
            "export_guard: redacted %d secret match(es) from export content: %s",
            len(pattern_types), sorted(set(pattern_types)),
        )
    return GuardedExport(payload=redacted, redacted_pattern_types=sorted(set(pattern_types)))


# ── 2. aggregation floor ────────────────────────────────────────────────────


def find_export_violations(payload: Any) -> List[Dict[str, str]]:
    """Return the aggregation-floor violations in ``payload`` ([] when clean).

    Non-raising counterpart to :func:`assert_export_safe`, for tests and for a
    caller that wants to report rather than refuse. Returns [] when the floor
    module is unavailable — callers that need the guarantee use
    :func:`assert_export_safe`, which logs that case loudly.
    """
    floor = _aggregation_floor()
    if floor is None:
        # Parity with assert_export_safe: an unavailable floor module is "cannot
        # check", not "clean". Returning [] silently would let a conformance test
        # asserting find_export_violations(payload) == [] pass VACUOUSLY while a
        # PII-dense payload goes unchecked — false assurance. Log it loudly so the
        # gap is visible; callers needing the hard guarantee use assert_export_safe.
        logger.warning(
            "export_guard: SecOps aggregation floor unavailable — find_export_violations "
            "returning [] WITHOUT the enumeration sweep (cannot check, not verified clean)"
        )
        return []
    return floor.find_output_violations(payload)


def _without_keys(payload: Any, exclude_keys: Sequence[str]) -> Any:
    """``payload`` with top-level-and-nested ``exclude_keys`` dropped.

    Used for the narrow, documented exclusions an export may legitimately need
    (the run's decision-audit actor identity — see ``evidence_export``). Applied
    recursively so an excluded key is dropped wherever the document nests it.
    """
    if not exclude_keys:
        return payload
    excluded = frozenset(exclude_keys)
    if isinstance(payload, Mapping):
        return {
            k: _without_keys(v, exclude_keys)
            for k, v in payload.items()
            if k not in excluded
        }
    if isinstance(payload, (list, tuple)):
        return [_without_keys(v, exclude_keys) for v in payload]
    return payload


def assert_export_safe(
    payload: Any,
    *,
    where: str,
    exclude_keys: Sequence[str] = (),
) -> None:
    """Raise :class:`ExportGuardViolation` unless ``payload`` holds the 1.9 floor.

    ``exclude_keys`` drops named keys from the swept view only (never from the
    exported payload). Every use must be justified at the call site: the floor
    flags any email as an individual reference, which is correct for finding
    content but wrong for an audit trail whose actor identity is the point.

    An unavailable floor module is logged at WARNING and treated as "cannot
    check" rather than "safe" — the export proceeds (refusing every export
    because an import failed would be its own outage) but the gap is visible.
    """
    floor = _aggregation_floor()
    if floor is None:
        logger.warning(
            "export_guard: SecOps aggregation floor unavailable — exporting %s "
            "WITHOUT the enumeration sweep", where,
        )
        return
    swept = _without_keys(payload, exclude_keys)
    violations = floor.find_output_violations(swept)
    if violations:
        raise ExportGuardViolation(
            f"export refused — {where} breaches the 1.9 SecOps aggregation floor "
            f"(no individual, host, CVE, or host x vulnerability pair may leave "
            f"the deployment): {violations}"
        )


# ── the one call an export surface makes ────────────────────────────────────


def guard_export_payload(
    payload: Any,
    *,
    where: str,
    strict: bool = False,
    floor_exclude_keys: Sequence[str] = (),
) -> GuardedExport:
    """Redact, then enforce the aggregation floor. Return the payload to export.

    This is the single entry point an export surface should call, immediately
    before it serialises/hashes/signs/writes. Running the two steps in this order
    matters: the floor sweeps the REDACTED content, so what is checked is what
    ships.

    Raises :class:`ExportGuardViolation` if the floor is breached — the caller
    must let that fail the export.
    """
    guarded = redact_export_content(payload, strict=strict)
    assert_export_safe(guarded.payload, where=where, exclude_keys=floor_exclude_keys)
    return guarded
