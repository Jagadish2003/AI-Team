"""
security_ops_finding.py — MSP-B12 T1: the four-part finding contract for the
Security Operations Discovery Pack, INHERITED from the operational pack scaffold
(MSP-B6's ``cloud_ops_finding.py``) and extended with the SecOps aggregation floor.

Every finding this pack emits carries the same four parts the operational pack
scaffold established (MSP-B12 §"four-part contract, inherited"):

  1. evidence       — the observed numbers/facts behind the finding.
  2. confidence     — LOW/MEDIUM/HIGH plus whether it is capped and why.
  3. corroboration  — which independent sources agree, or an explicit single-source
                      cap.
  4. source_trace   — the originating systems and, crucially for B12, a trace back
                      to source records through VALID EVIDENCE POINTERS (the R16-B1
                      provenance spine).

The four-part vocabulary, builders, "no individuals" sweep, and causal-gate are
imported from ``cloud_ops_finding`` unchanged — B12 is B6's pattern applied to a
second domain, so the contract is inherited, not re-copied. This module adds ONLY
what is SecOps-specific:

  * The AGGREGATION FLOOR at the finding layer (MSP-B12 §"aggregation floor" /
    AC2/AC7): no finding may name an individual employee (inherited) OR expose an
    individual host / asset or an individual host x vulnerability pair. Findings
    describe groups, queues, vulnerability classes, services, CI classes,
    workload concentration, recurrence, and ageing — never an individual record.
    Individual records are reachable only through access-controlled evidence
    pointers, one at a time (the logged resolution path is T3).

  * Evidence-pointer validation on the source_trace, so "trace back to its source
    records through valid evidence pointers" is enforceable, not aspirational.

This module contains NO detector or scorer logic — only contract construction and
the SecOps boundary guarantees.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# ── Inherit the four-part contract from the operational pack scaffold (MSP-B6) ──
# B12 is the second sibling of the cloud-operations pack on the same template
# model. The four-part contract, its builders, the "no individuals" sweep, and the
# causal-gate are the SAME shape — imported, never re-copied, so the two
# operational packs cannot drift.
try:  # package-qualified first, bare fallback (mirrors the other pack modules)
    from backend.discovery.packs.cloud_ops_finding import (  # noqa: F401
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
        find_causal_language,
        find_individual_references,
        is_contract_complete,
        missing_contract_parts,
    )
    from backend.app.provenance import EvidencePointer
except ModuleNotFoundError:  # pragma: no cover - import shim
    from discovery.packs.cloud_ops_finding import (  # noqa: F401
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
        find_causal_language,
        find_individual_references,
        is_contract_complete,
        missing_contract_parts,
    )
    from app.provenance import EvidencePointer


# ── SecOps aggregation floor: no individual host / asset / host×vuln pair ────────
#
# The signal layer (MSP-B11) already enforces "workload, not weakness". This is the
# SECOND enforcement — belt at the signal layer, braces here at the finding layer —
# because a readout deck that doubles as a target list would be a catastrophic
# failure in a federal room (MSP-B12 §"aggregation floor").
#
# Findings speak GROUPS, QUEUES, VULNERABILITY CLASSES, SERVICES, and CI CLASSES.
# They must never carry a field that identifies an INDIVIDUAL HOST / ASSET, nor an
# individual vulnerability-INSTANCE id. A CVE (which names a vulnerability CLASS) is
# NOT denied — the forbidden thing is the host×vulnerability PAIR, and because an
# individual host/asset identifier is denied outright, no pair can form.

HOST_ASSET_FIELD_DENYLIST = frozenset({
    "host",
    "hostname",
    "host_name",
    "fqdn",
    "ip",
    "ip_address",
    "ipv4",
    "ipv6",
    "asset",
    "asset_id",
    "asset_tag",
    "asset_name",
    "device",
    "device_id",
    "device_name",
    "mac",
    "mac_address",
    "instance_id",
    "node",
    "node_id",
    "server",
    "server_name",
    "endpoint_id",
})

# A specific scanner finding / vulnerability INSTANCE — distinct from a vulnerability
# CLASS (which findings may name, e.g. by CVE). Enumerating instances is the target
# list the aggregation floor forbids.
VULN_INSTANCE_FIELD_DENYLIST = frozenset({
    "vulnerability_id",
    "vuln_id",
    "vulnerability_instance_id",
    "vuln_instance_id",
    "qid",
    "plugin_id",
    "finding_id",
    "scan_finding_id",
    "detection_id",
})


def _denylist_hits(obj: Any, denylist, *, _path: str = "") -> List[str]:
    """Recursively collect dotted paths where a denylisted key carries a value."""
    hits: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{_path}.{key}" if _path else str(key)
            if str(key).lower() in denylist and value not in (None, "", [], {}):
                hits.append(here)
            hits.extend(_denylist_hits(value, denylist, _path=here))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            hits.extend(_denylist_hits(value, denylist, _path=f"{_path}[{i}]"))
    return hits


def find_host_or_asset_references(obj: Any) -> List[str]:
    """Return dotted paths to any individual host/asset identifier ([] when clean)."""
    return _denylist_hits(obj, HOST_ASSET_FIELD_DENYLIST)


def find_vulnerability_instance_references(obj: Any) -> List[str]:
    """Return dotted paths to any individual vulnerability-instance id ([] when clean)."""
    return _denylist_hits(obj, VULN_INSTANCE_FIELD_DENYLIST)


def find_aggregation_floor_violations(obj: Any) -> List[str]:
    """Return every aggregation-floor violation on ``obj`` (MSP-B12 AC2/AC7).

    Combines the inherited "no individuals" sweep with the SecOps host/asset and
    vulnerability-instance sweeps — the single definition the boundary enforcement
    and the sweep test run over an emitted finding / any pack output surface.
    """
    hits: List[str] = []
    hits.extend(find_individual_references(obj))
    hits.extend(find_host_or_asset_references(obj))
    hits.extend(find_vulnerability_instance_references(obj))
    return hits


def assert_aggregation_floor(obj: Any) -> None:
    """Raise ValueError if ``obj`` violates the aggregation floor (used by tests/T3)."""
    hits = find_aggregation_floor_violations(obj)
    if hits:
        raise ValueError(
            f"finding violates the SecOps aggregation floor (groups/queues/"
            f"vulnerability-classes/services/CI-classes only — no individual "
            f"employee, host, or host x vulnerability pair): {hits}"
        )


# ── Evidence-pointer validation (MSP-B12 four-part "source trace") ──────────────


def _pointer_of(artifact: Any) -> Optional[Dict[str, Any]]:
    """Extract the evidence-pointer dict from a source-trace artifact, if present.

    An artifact may BE a pointer (carry the R16-B1 mandatory spine directly) or
    WRAP one under ``evidence_pointer`` / ``pointer`` / ``provenance``.
    """
    if not isinstance(artifact, dict):
        return None
    for key in ("evidence_pointer", "pointer", "provenance"):
        nested = artifact.get(key)
        if isinstance(nested, dict):
            return nested
    spine = ("source_system", "source_artifact", "source_timestamp", "origin")
    if all(artifact.get(k) for k in spine):
        return artifact
    return None


def find_invalid_evidence_pointers(source_trace: Any) -> List[str]:
    """Return a description for each source-trace artifact lacking a valid pointer.

    Every artifact in the trace must resolve to a valid R16-B1 EvidencePointer
    (mandatory spine present; inferred pointers name their extraction job). Returns
    [] when every artifact carries a valid pointer.
    """
    if not isinstance(source_trace, dict):
        return ["source_trace is not a mapping"]
    artifacts = source_trace.get("artifacts")
    if not artifacts:
        return ["source_trace carries no artifacts to trace back to"]
    bad: List[str] = []
    for i, artifact in enumerate(artifacts):
        pointer = _pointer_of(artifact)
        if pointer is None:
            bad.append(f"artifacts[{i}] carries no evidence pointer")
            continue
        try:
            valid = EvidencePointer.from_dict(pointer).is_valid()
        except TypeError:
            # from_dict raises when a mandatory spine field is absent (the fields
            # have no defaults) — that is precisely an invalid pointer.
            valid = False
        if not valid:
            bad.append(f"artifacts[{i}] evidence pointer is not valid")
    return bad


def build_source_trace(
    *,
    systems: Sequence[str],
    artifacts: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the source-trace part and validate its evidence pointers.

    systems   — originating systems the finding resolves to (e.g. ["servicenow"]).
    artifacts — pointers to concrete source records. Each MUST resolve to a valid
                evidence pointer (the B12 tightening of the inherited trace).
    """
    trace = {
        "systems": list(systems),
        "artifacts": [dict(a) for a in artifacts],
    }
    bad = find_invalid_evidence_pointers(trace)
    if bad:
        raise ValueError(f"finding source_trace has invalid evidence pointer(s): {bad}")
    return trace


# ── Four-part contract assembly (inherited shape, SecOps floor enforced) ─────────


def build_finding_contract(
    *,
    evidence: Dict[str, Any],
    confidence: Dict[str, Any],
    corroboration: Dict[str, Any],
    source_trace: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the four-part contract and fail loudly on any violation.

    Enforces the inherited invariants (all four parts non-empty; evidence carries a
    numeric value; source_trace resolves to a system and artifact) PLUS the SecOps
    aggregation floor and evidence-pointer validity.
    """
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("finding contract 'evidence' must be a non-empty dict")
    if not any(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        for v in _flatten(evidence)
    ):
        raise ValueError("finding contract 'evidence' must contain a numeric value")
    for name, part in (("confidence", confidence), ("corroboration", corroboration)):
        if not isinstance(part, dict) or not part:
            raise ValueError(f"finding contract {name!r} must be a non-empty dict")
    if not source_trace.get("systems") or not source_trace.get("artifacts"):
        raise ValueError("finding contract 'source_trace' must resolve to a system and artifact")

    bad_pointers = find_invalid_evidence_pointers(source_trace)
    if bad_pointers:
        raise ValueError(f"finding contract 'source_trace' has invalid evidence pointer(s): {bad_pointers}")

    contract = {
        "evidence": evidence,
        "confidence": confidence,
        "corroboration": corroboration,
        "source_trace": source_trace,
    }
    # Defence in depth: a finding must never breach the aggregation floor.
    leaked = find_aggregation_floor_violations(contract)
    if leaked:
        raise ValueError(
            f"finding contract breaches the SecOps aggregation floor (groups/queues/"
            f"vulnerability-classes/services/CI-classes only): {leaked}"
        )
    return contract


# ── Pack-boundary enforcement (MSP-B12 T1 — inherited four-part + SecOps floor) ──
#
# The four-part criterion is enforced HERE, at the pack boundary: a finding this
# pack emits that is missing any of the four parts, breaches the aggregation floor,
# or carries an invalid evidence pointer is a CONTRACT VIOLATION that fails the
# run's pack execution — never a cosmetic gap. This is the inherited B6 boundary
# guarantee, extended with the SecOps floor, and it is what the boundary test drives
# to prove a future detector cannot ship a non-conforming finding unnoticed.


class SecurityOpsContractViolation(ValueError):
    """A Security-Operations finding failed the four-part contract or aggregation
    floor at the pack boundary. Raised to FAIL the run's pack execution."""


def _finding_contract_of(result: Any) -> Any:
    """Extract the four-part contract from a DetectorResult-like object or dict."""
    raw = getattr(result, "raw_evidence", None)
    if raw is None and isinstance(result, dict):
        raw = result.get("raw_evidence", result)
    if not isinstance(raw, dict):
        return None
    return raw.get("finding_contract")


def enforce_finding_contract(
    contract: Any,
    *,
    detector_id: str = "",
    index: Optional[int] = None,
) -> None:
    """Raise :class:`SecurityOpsContractViolation` unless ``contract`` carries all
    four non-empty parts, honours the aggregation floor, and traces back through
    valid evidence pointers. No-op when valid."""
    where = f"detector {detector_id!r}" if detector_id else "finding"
    if index is not None:
        where += f" (index {index})"

    if contract is None:
        raise SecurityOpsContractViolation(
            f"{where} carries no four-part finding_contract — every Security-Operations "
            f"finding must carry {list(FOUR_PART_CONTRACT_FIELDS)}."
        )
    missing = missing_contract_parts(contract)
    if missing:
        raise SecurityOpsContractViolation(
            f"{where} is missing required contract part(s) {missing}; a finding must "
            f"carry all four: {list(FOUR_PART_CONTRACT_FIELDS)}."
        )
    leaked = find_aggregation_floor_violations(contract)
    if leaked:
        raise SecurityOpsContractViolation(
            f"{where} breaches the SecOps aggregation floor (groups/queues/vulnerability-"
            f"classes/services/CI-classes only — no individual employee, host, or "
            f"host x vulnerability pair): {leaked}."
        )
    bad_pointers = find_invalid_evidence_pointers(contract.get("source_trace"))
    if bad_pointers:
        raise SecurityOpsContractViolation(
            f"{where} does not trace back through valid evidence pointers: {bad_pointers}."
        )


def enforce_pack_findings(results: Sequence[Any]) -> int:
    """Enforce the four-part contract + aggregation floor across every emitted
    finding at the pack boundary. Raises :class:`SecurityOpsContractViolation` on
    the first violation (failing the run). Returns the number of findings validated."""
    count = 0
    for i, result in enumerate(results or []):
        detector_id = str(getattr(result, "detector_id", "") or "")
        enforce_finding_contract(
            _finding_contract_of(result), detector_id=detector_id, index=i
        )
        count += 1
    return count


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
