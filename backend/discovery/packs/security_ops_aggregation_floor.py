"""
security_ops_aggregation_floor.py — MSP-B12 T3: the mandatory aggregation boundary
for EVERY output the Security Operations pack produces.

This is the SECOND enforcement layer after MSP-B11's field/signal controls (belt at
the signal layer, braces here at the finding/output layer). Where the four-part
contract (``security_ops_finding``) guards the shape of a single finding, THIS
module is the recursive sweep applied to every serialized pack output surface —
finding titles, explanations, raw evidence, evidence summaries, reports, exports,
and telemetry payloads — because filtering only the visible title is insufficient:
a host×vulnerability pair hidden in a nested evidence field is just as much a target
list as one in the title.

Allowed (safe) aggregation dimensions — vulnerability class, service, CI class,
assignment group, remediation path, severity band, queue, deferral class, incident
category — are group-level and pass cleanly. Prohibited:

  * an individual-employee / person field or an email in any string;
  * an individual host / asset identifier (field OR an IP/MAC in free text);
  * an individual vulnerability instance (field) or a specific CVE in free text;
  * a host×vulnerability PAIR (a host identity and a vulnerability identity
    co-occurring — the "list pairing hosts with vulnerabilities" the floor forbids).

Individual source records remain reachable ONLY through the org-scoped, access-
controlled, audited evidence pointers resolved by ``security_ops_evidence_resolver``
— never by embedding record content in an output.

The field-level denylists and sweeps are inherited from ``security_ops_finding``
(one source of truth); this module adds the free-text and pair detection and the
single ``assert_output_safe`` / ``enforce_pack_output`` entry points.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

try:  # package-qualified first, bare fallback (mirrors the other pack modules)
    from backend.discovery.packs import security_ops_finding as _fnd
except ModuleNotFoundError:  # pragma: no cover - import shim
    from discovery.packs import security_ops_finding as _fnd


class SecOpsAggregationFloorViolation(ValueError):
    """A Security-Operations output breached the aggregation floor. Raised to FAIL
    the pack execution (never swallowed) — a readout that doubles as a target list
    is a catastrophic failure in a federal room."""


# ── Free-text identity patterns ─────────────────────────────────────────────────
# A specific CVE identifies an individual vulnerability; an IP/MAC identifies an
# individual host. A vulnerability CLASS (e.g. "missing patch") and a CI CLASS
# (e.g. "cmdb_ci_server") are group-level and are NOT matched here.
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{3,}\b", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")


def _has_ipv4(text: str) -> bool:
    for match in _IPV4_RE.finditer(text):
        if all(0 <= int(octet) <= 255 for octet in match.groups()):
            return True
    return False


def _string_identities(text: str) -> Dict[str, bool]:
    """Which individual identities a single string exposes (host / vulnerability)."""
    has_cve = bool(_CVE_RE.search(text))
    has_host = _has_ipv4(text) or bool(_MAC_RE.search(text))
    return {"cve": has_cve, "host": has_host}


def _scan_strings(obj: Any, *, _path: str = "") -> List[Dict[str, str]]:
    """Recursively flag prohibited identities embedded in ANY string value."""
    hits: List[Dict[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{_path}.{key}" if _path else str(key)
            hits.extend(_scan_strings(value, _path=here))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            hits.extend(_scan_strings(value, _path=f"{_path}[{i}]"))
    elif isinstance(obj, str):
        ids = _string_identities(obj)
        if ids["cve"] and ids["host"]:
            hits.append({"path": _path, "kind": "host_vuln_pair_in_text",
                         "detail": "a host identity and a CVE co-occur in one string (target-list pairing)"})
        elif ids["cve"]:
            hits.append({"path": _path, "kind": "cve_in_text",
                         "detail": "a specific CVE (individual vulnerability) appears in free text"})
        elif ids["host"]:
            hits.append({"path": _path, "kind": "host_in_text",
                         "detail": "an individual host identity (IP/MAC) appears in free text"})
    return hits


# ── The single output sweep ──────────────────────────────────────────────────────


def find_output_violations(obj: Any) -> List[Dict[str, str]]:
    """Recursively inspect ``obj`` (any serialized pack output) and return every
    aggregation-floor violation ([] when clean).

    Combines the inherited field-level sweeps (individual/host/vuln-instance fields
    + email) with this module's free-text CVE/IP/MAC and host×vulnerability-pair
    detection, so no surface — title, explanation, nested evidence, report cell,
    export row, or telemetry field — can carry a prohibited combination.
    """
    violations: List[Dict[str, str]] = []
    for path in _fnd.find_individual_references(obj):
        violations.append({"path": path, "kind": "individual",
                           "detail": "individual-person field or email"})
    for path in _fnd.find_host_or_asset_references(obj):
        violations.append({"path": path, "kind": "host_field",
                           "detail": "individual host/asset identifier field"})
    for path in _fnd.find_vulnerability_instance_references(obj):
        violations.append({"path": path, "kind": "vuln_instance_field",
                           "detail": "individual vulnerability-instance identifier field"})
    violations.extend(_scan_strings(obj))
    # Deterministic ordering for stable reporting.
    violations.sort(key=lambda v: (v["path"], v["kind"]))
    return violations


def assert_output_safe(obj: Any, *, where: str = "output") -> None:
    """Raise :class:`SecOpsAggregationFloorViolation` if ``obj`` breaches the floor.

    The one call every SecOps serialization boundary uses — findings, evidence
    summaries, reports, exports, and telemetry payloads all pass through here.
    """
    violations = find_output_violations(obj)
    if violations:
        raise SecOpsAggregationFloorViolation(
            f"{where} breaches the SecOps aggregation floor "
            f"(groups/queues/vulnerability-classes/services/CI-classes only — no "
            f"individual employee, host, CVE, or host×vulnerability pair): {violations}"
        )


def _payload_of(result: Any) -> Any:
    """The full serialized payload of a detector result (or the object itself)."""
    payload = getattr(result, "raw_evidence", None)
    if payload is None and isinstance(result, dict):
        payload = result.get("raw_evidence", result)
    return payload if payload is not None else result


def enforce_pack_output(results: Any) -> int:
    """Sweep every emitted finding's FULL serialized payload at the pack boundary.

    Raises :class:`SecOpsAggregationFloorViolation` on the first violation (failing
    the run). Returns the number of outputs swept. This is the mandatory boundary
    that guarantees no pack output — visible or hidden — carries a prohibited
    host×vulnerability pair or person-level datum (MSP-B12 T3 / AC2).
    """
    count = 0
    for i, result in enumerate(results or []):
        detector_id = str(getattr(result, "detector_id", "") or "")
        where = f"detector {detector_id!r} (index {i})" if detector_id else f"output {i}"
        assert_output_safe(_payload_of(result), where=where)
        count += 1
    return count
