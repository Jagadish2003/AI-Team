"""
evidence_export.py — Release 2.0-B1 T4: signed evidence export (AC4).

Produces an offline, self-contained, SIGNED bundle for one finding or for a
whole run's report: the finding's full provenance trace (T1/T2), the evidence
records it references, the evidence-pointer spine, and the run + pack versions
that produced it — for auditors, regulators, and board packs.

AC4: "Export bundle verifies against its signature; altering any byte fails
verification." That is delivered by REUSING the repo's existing signing
primitives rather than inventing a second scheme — ``app.usage_report`` already
owns the only signing capability the installed product has:

  * ``canonical_bytes``   — deterministic serialisation (sorted keys, compact
                            separators, UTF-8) so signer and verifier agree
                            byte-for-byte.
  * ``sign_report_body``  — HMAC-SHA256 over those bytes.
  * ``verify_report``     — recompute + ``hmac.compare_digest`` (constant time).

The key is the per-installation ``report_key`` carried in the Ed25519-signed
license payload (L1), resolved through the SAME resolver the usage report uses
so there is exactly one key-resolution implementation for signed artifacts. No
signing-key env var is introduced. Asymmetric signing is deliberately NOT used:
the CloudFulcrum Ed25519 PRIVATE key never ships inside the product
(``licensing.py``), so a runtime export cannot produce an asymmetric signature.
A verifier therefore needs the installation's ``report_key`` — the same trust
model as the signed usage report.

Two layers of tamper evidence, both inside the signed body:

  1. The HMAC signature over the whole canonical body — ANY altered byte
     anywhere fails ``verify_export_envelope`` (AC4).
  2. An ``integrity`` block: a per-record ``content_hash`` folded into a
     ``content_root`` (the ``billing_chain`` pattern). The signature alone says
     "something changed"; this says WHICH record changed. It is inside the
     signed body, so it cannot be edited to match a forged record without
     also breaking the signature.

Two disciplines applied BEFORE signing, so what is signed is what is exported:

  * The SecOps aggregation floor (``security_ops_aggregation_floor``) — its own
    docstring names exports as a covered surface. A violation FAILS the export
    loudly; a signed readout that doubles as a host x vulnerability target list
    would be a catastrophic artifact to hand to a third party.
  * Secret redaction (``secret_redaction``) as defence in depth. Retrieval
    content is already redacted upstream ("redact before index, always"), but
    detector-built evidence snippets and narrative prose are not, and this
    bundle leaves the deployment. (These two support sibling AC5; this ticket
    owns AC4.)

Determinism note: the bundle is built from the RAW run-scoped KV values, never
from the display-shaped reads (``apply_run_terminology`` / ``with_display*``).
Terminology templates can change, which would silently break re-verification of
a previously issued bundle.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import db
from .usage_report import (
    SIGNATURE_ALGORITHM,
    UsageReportError,
    canonical_bytes,
    sign_report_body,
    verify_report,
)

logger = logging.getLogger(__name__)

BUNDLE_VERSION = 1
INTEGRITY_ALGORITHM = "sha256/record-hash-chain"

SCOPE_FINDING = "finding"
SCOPE_REPORT = "report"
VALID_SCOPES = (SCOPE_FINDING, SCOPE_REPORT)

# Report-scope bundles cover every finding in a run. A cap keeps one request
# from assembling an unbounded document (and keeps the recursive aggregation
# floor sweep bounded); exceeding it is reported LOUDLY on the bundle rather
# than silently truncating an artifact someone will audit.
MAX_REPORT_FINDINGS = 200


class EvidenceExportError(Exception):
    """Raised when a signed export cannot be produced (no license report_key,
    unknown run/opportunity, or a content-discipline violation). The caller maps
    this to an HTTP error — an unsigned or unsafe bundle is NEVER returned."""


# ── content discipline (runs before hashing + signing) ───────────────────────


def _guard_export_content(payload: Any, *, where: str) -> Tuple[Any, List[str]]:
    """Redact secrets, then enforce the 1.9 aggregation floor, on bundle content.

    Delegates to :mod:`app.export_guard` — the ONE shared implementation every
    export path in the product routes through (2.0-B1 T5 / AC5), so this bundle
    and every other export cannot drift apart on either discipline. The floor
    violation is re-raised as an :class:`EvidenceExportError` to keep this
    module's public error contract (and the route's status mapping) unchanged.

    ``audit`` is excluded from the FLOOR sweep only (never from the exported
    payload): the run decision audit's ``by`` field is the analyst who recorded a
    decision. That actor identity is the entire point of an audit trail and an
    auditor requires it, but the floor flags any email as an individual
    reference — sweeping it would either strip the attestation's provenance or
    make every reviewed run unexportable. The audit list has a fixed shape
    (id / timestamps / action / actor / evidence id) and so cannot carry a
    host x vulnerability enumeration, which is what the floor exists to stop.
    Secret redaction still covers it.
    """
    from .export_guard import ExportGuardViolation, guard_export_payload

    try:
        guarded = guard_export_payload(
            payload,
            where=where,
            # Base pattern set: this bundle carries the run's decision audit,
            # whose actor email must survive (the strict set would scrub it).
            strict=False,
            floor_exclude_keys=("audit",),
        )
    except ExportGuardViolation as exc:
        raise EvidenceExportError(str(exc)) from exc
    return guarded.payload, guarded.redacted_pattern_types


# ── integrity block (per-record hashes folded into a root) ───────────────────


def _canonical_str(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(kind: str, record_id: str, content: Any) -> str:
    """Hash binding one exported record's kind, id, and content."""
    return hashlib.sha256(
        f"{kind}\n{record_id}\n{_canonical_str(content)}".encode("utf-8")
    ).hexdigest()


def _fold(prev_root: str, this_hash: str) -> str:
    return hashlib.sha256(f"{prev_root}\n{this_hash}".encode("utf-8")).hexdigest()


def build_integrity_block(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the integrity block over ``records``.

    Each item is ``{"kind": str, "record_id": str, "content": Any}``. Records are
    ordered deterministically by ``(kind, record_id)`` — never by input order —
    so the same bundle always yields the same root. Returns the per-record
    hashes plus the folded ``content_root``.
    """
    ordered = sorted(
        records, key=lambda r: (str(r.get("kind") or ""), str(r.get("record_id") or ""))
    )
    entries: List[Dict[str, str]] = []
    root = ""
    for item in ordered:
        kind = str(item.get("kind") or "")
        record_id = str(item.get("record_id") or "")
        content_hash = record_hash(kind, record_id, item.get("content"))
        root = _fold(root, content_hash)
        entries.append({
            "kind": kind,
            "record_id": record_id,
            "content_hash": content_hash,
            "chain_hash": root,
        })
    return {
        "algorithm": INTEGRITY_ALGORITHM,
        "record_count": len(entries),
        "records": entries,
        "content_root": root,
    }


# ── bundle assembly ─────────────────────────────────────────────────────────


def _run_provenance(run: Mapping[str, Any]) -> Dict[str, Any]:
    """The run + pack version provenance the bundle attests to.

    The multi-pack fields (``packIds``/``packVersions``/``packs``) are written
    only for multi-pack runs, so each is read defensively — an older single-pack
    run carries just ``packId``/``packVersion``.
    """
    return {
        "run_id": run.get("id") or run.get("runId"),
        "started_at": run.get("startedAt") or run.get("started_at"),
        "completed_at": run.get("completedAt") or run.get("completed_at"),
        "mode": run.get("mode"),
        "pack_id": run.get("packId"),
        "pack_name": run.get("packName"),
        "pack_version": run.get("packVersion"),
        "pack_ids": run.get("packIds") if isinstance(run.get("packIds"), list) else None,
        "pack_versions": (
            run.get("packVersions") if isinstance(run.get("packVersions"), dict) else None
        ),
        "packs": run.get("packs") if isinstance(run.get("packs"), list) else None,
        "executed_detector_ids": (
            run.get("executedDetectorIds")
            if isinstance(run.get("executedDetectorIds"), list)
            else None
        ),
        "pack_executed_at": run.get("packExecutedAt"),
    }


def _finding_section(
    opportunity: Mapping[str, Any],
    run_id: str,
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Assemble one finding's section: the opportunity, its trace, the evidence
    records it references (in the opportunity's own order), and its pointers."""
    opp_id = str(opportunity.get("id") or "")

    trace_dict: Optional[Dict[str, Any]] = None
    try:
        from .trace_graph import load_finding_trace

        trace = load_finding_trace(run_id, opp_id)
        trace_dict = trace.to_dict() if trace is not None else None
    except Exception as exc:  # noqa: BLE001 — a missing trace must not void the export.
        logger.warning(
            "evidence_export: trace unavailable for run=%s opp=%s: %s", run_id, opp_id, exc
        )
        trace_dict = None

    ordered_ids = [str(eid) for eid in (opportunity.get("evidenceIds") or []) if eid]
    evidence = [dict(evidence_by_id[eid]) for eid in ordered_ids if eid in evidence_by_id]
    missing = [eid for eid in ordered_ids if eid not in evidence_by_id]

    pointers: List[Dict[str, Any]] = []
    try:
        from .evidence_pointers import get_evidence_pointers_for_opportunity

        pointers = [dict(p) for p in get_evidence_pointers_for_opportunity(run_id, opp_id)]
    except Exception as exc:  # noqa: BLE001 — pointers are advisory here.
        logger.debug(
            "evidence_export: pointers unavailable for run=%s opp=%s: %s", run_id, opp_id, exc
        )

    return {
        "opportunity_id": opp_id,
        "opportunity": dict(opportunity),
        "trace": trace_dict,
        "evidence": evidence,
        "evidence_pointers": pointers,
        # Referenced-but-absent evidence ids are stated, never quietly dropped —
        # an auditor must be able to see the bundle is not silently partial.
        "missing_evidence_ids": missing,
    }


def _load_run(run_id: str) -> Mapping[str, Any]:
    run = db.get_run(run_id)
    if not isinstance(run, Mapping):
        raise EvidenceExportError(f"run '{run_id}' not found")
    return run


def _run_org_id(run: Mapping[str, Any]) -> Optional[str]:
    """The run's own org stamp, or None for a pre-stamp run."""
    inputs = run.get("inputs") if isinstance(run.get("inputs"), Mapping) else {}
    for candidate in (
        run.get("org_id"),
        run.get("orgId"),
        inputs.get("org_id"),
        inputs.get("orgId"),
    ):
        if candidate:
            return str(candidate)
    return None


def _load_opps_and_evidence(
    run_id: str,
) -> Tuple[List[Mapping[str, Any]], Dict[str, Mapping[str, Any]]]:
    opps = db.run_kv_get("opps", run_id, []) or []
    opps = [o for o in opps if isinstance(o, Mapping)]
    evidence_items = db.run_kv_get("evidence", run_id, []) or []
    evidence_by_id = {
        str(ev.get("id")): ev
        for ev in evidence_items
        if isinstance(ev, Mapping) and ev.get("id")
    }
    return opps, evidence_by_id


def build_export_bundle(
    org_id: str,
    run_id: str,
    *,
    scope: str = SCOPE_FINDING,
    opp_id: Optional[str] = None,
    kid: Optional[str] = None,
    license_org_id: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the (unsigned) export bundle body.

    ``scope=SCOPE_FINDING`` requires ``opp_id`` and covers that one finding;
    ``scope=SCOPE_REPORT`` covers every finding in the run plus the run's
    executive report, roadmap, and decision audit.

    Raises :class:`EvidenceExportError` for an unknown run, an opportunity absent
    from the run, an invalid scope, or a content-discipline violation.
    """
    if scope not in VALID_SCOPES:
        raise EvidenceExportError(
            f"scope must be one of {list(VALID_SCOPES)}; got {scope!r}"
        )
    if scope == SCOPE_FINDING and not opp_id:
        raise EvidenceExportError("a finding-scoped export requires an opportunity id")

    run = _load_run(run_id)
    # Defence in depth for direct programmatic callers (CLI, a future background
    # job) that do not pass through the API's _require_run_in_org gate: a signed
    # bundle must never attest that a run belongs to an org it does not. A missing
    # run org stamp is refused too — an unconfirmable owner must not be signed over.
    run_org = _run_org_id(run)
    if not run_org or run_org != org_id:
        raise EvidenceExportError(f"run '{run_id}' not found")
    opps, evidence_by_id = _load_opps_and_evidence(run_id)

    if scope == SCOPE_FINDING:
        opportunity = next((o for o in opps if o.get("id") == opp_id), None)
        if opportunity is None:
            raise EvidenceExportError(
                f"opportunity '{opp_id}' not found in run '{run_id}'"
            )
        selected = [opportunity]
        truncated = False
    else:
        selected = opps[:MAX_REPORT_FINDINGS]
        truncated = len(opps) > MAX_REPORT_FINDINGS
        if truncated:
            logger.warning(
                "evidence_export: run %s has %d findings — bundling the first %d "
                "(reported on the bundle, not silently truncated)",
                run_id, len(opps), MAX_REPORT_FINDINGS,
            )

    findings = [_finding_section(o, run_id, evidence_by_id) for o in selected]

    report_artifacts: Optional[Dict[str, Any]] = None
    if scope == SCOPE_REPORT:
        # RAW KV values — never the display-shaped route reads (see docstring).
        report_artifacts = {
            "executive_report": db.run_kv_get("executive_report", run_id, None),
            "roadmap": db.run_kv_get("roadmap", run_id, None),
            "audit": db.run_kv_get("audit", run_id, []) or [],
        }

    # Content discipline BEFORE hashing/signing so signed == exported: redact
    # secrets, then enforce the 1.9 aggregation floor, via the shared export
    # guard every export path in the product routes through (T5 / AC5).
    content: Dict[str, Any] = {"findings": findings}
    if report_artifacts is not None:
        content["report_artifacts"] = report_artifacts
    content, redacted_patterns = _guard_export_content(
        content, where=f"signed evidence export ({scope})"
    )

    findings = content["findings"]
    report_artifacts = content.get("report_artifacts")

    records: List[Dict[str, Any]] = [
        {"kind": "run_provenance", "record_id": str(run_id), "content": _run_provenance(run)},
    ]
    for section in findings:
        fid = str(section.get("opportunity_id") or "")
        records.append({"kind": "opportunity", "record_id": fid, "content": section.get("opportunity")})
        records.append({"kind": "trace", "record_id": fid, "content": section.get("trace")})
        records.append({
            "kind": "evidence_pointers", "record_id": fid,
            "content": section.get("evidence_pointers"),
        })
        for ev in section.get("evidence") or []:
            records.append({
                "kind": "evidence", "record_id": str(ev.get("id") or ""), "content": ev,
            })
    if report_artifacts is not None:
        for name, value in sorted(report_artifacts.items()):
            records.append({"kind": name, "record_id": str(run_id), "content": value})

    body: Dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "scope": scope,
        "org_id": org_id,
        "run_id": run_id,
        "opportunity_id": opp_id if scope == SCOPE_FINDING else None,
        "generated_at": generated_at
        or _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "license": {"kid": kid, "org_id": license_org_id},
        "run_provenance": _run_provenance(run),
        "findings": findings,
        "finding_count": len(findings),
        "truncated": truncated,
        "redacted_pattern_types": sorted(set(redacted_patterns)),
    }
    if report_artifacts is not None:
        body["report_artifacts"] = report_artifacts
    body["integrity"] = build_integrity_block(records)
    return body


# ── sign / verify ───────────────────────────────────────────────────────────


def _resolve_signing_key(org_id: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Resolve ``(report_key, kid, license_org_id)`` for signing.

    Delegates to the usage report's resolver so signed artifacts share ONE
    key-resolution implementation (duplicating security-critical key handling is
    how the two drift apart). Its :class:`UsageReportError` is re-raised as an
    :class:`EvidenceExportError` so callers handle a single exception type.
    """
    from .usage_report import _resolve_license_signing

    try:
        return _resolve_license_signing(org_id)
    except UsageReportError as exc:
        raise EvidenceExportError(
            f"a signed evidence export cannot be produced: {exc}"
        ) from exc


def generate_signed_export(
    org_id: str,
    run_id: str,
    *,
    scope: str = SCOPE_FINDING,
    opp_id: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce the signed export envelope.

    Returns ``{"bundle": <body>, "signature": <hex>, "algorithm": "HMAC-SHA256"}``.
    Fully local — no outbound call. Raises :class:`EvidenceExportError` rather
    than ever returning an unsigned bundle.
    """
    report_key, kid, license_org_id = _resolve_signing_key(org_id)
    body = build_export_bundle(
        org_id,
        run_id,
        scope=scope,
        opp_id=opp_id,
        kid=kid,
        license_org_id=license_org_id,
        generated_at=generated_at,
    )
    return {
        "bundle": body,
        "signature": sign_report_body(body, report_key),
        "algorithm": SIGNATURE_ALGORITHM,
    }


def verify_export_envelope(envelope: Any, report_key: str) -> Dict[str, Any]:
    """Independently verify a signed export bundle (AC4).

    Checks, in order:
      * the envelope is well formed and names the expected algorithm;
      * the HMAC signature matches the canonical bytes of ``envelope["bundle"]``
        under ``report_key`` — ANY altered byte anywhere fails here;
      * the integrity block re-folds to its recorded ``content_root``, so a
        bundle whose signature was stripped/replaced still shows internal
        inconsistency, and a mismatch localises which record changed.

    Returns ``{"verified", "signature_valid", "integrity_consistent",
    "content_root_matches", "reason"}``. Never raises — a malformed envelope is
    reported as unverified, which is what a verifier needs.
    """
    verdict: Dict[str, Any] = {
        "verified": False,
        "signature_valid": False,
        "integrity_consistent": False,
        "content_root_matches": False,
        "reason": "",
    }
    if not isinstance(envelope, Mapping):
        verdict["reason"] = "envelope is not an object"
        return verdict

    body = envelope.get("bundle")
    signature = envelope.get("signature")
    algorithm = envelope.get("algorithm")
    if not isinstance(body, Mapping):
        verdict["reason"] = "envelope carries no bundle object"
        return verdict
    if not isinstance(signature, str) or not signature:
        verdict["reason"] = "envelope carries no signature"
        return verdict
    if algorithm != SIGNATURE_ALGORITHM:
        verdict["reason"] = (
            f"unexpected signature algorithm {algorithm!r} "
            f"(expected {SIGNATURE_ALGORITHM})"
        )
        return verdict

    verdict["signature_valid"] = verify_report(dict(body), signature, report_key)

    integrity = body.get("integrity")
    if isinstance(integrity, Mapping):
        entries = integrity.get("records")
        entries = entries if isinstance(entries, list) else []
        root = ""
        consistent = True
        for entry in entries:
            if not isinstance(entry, Mapping):
                consistent = False
                break
            content_hash = str(entry.get("content_hash") or "")
            root = _fold(root, content_hash)
            if str(entry.get("chain_hash") or "") != root:
                consistent = False
                break
        verdict["integrity_consistent"] = consistent
        verdict["content_root_matches"] = consistent and root == str(
            integrity.get("content_root") or ""
        )
    else:
        verdict["reason"] = "bundle carries no integrity block"

    verdict["verified"] = bool(
        verdict["signature_valid"]
        and verdict["integrity_consistent"]
        and verdict["content_root_matches"]
    )
    if not verdict["verified"] and not verdict["reason"]:
        if not verdict["signature_valid"]:
            verdict["reason"] = "signature does not match the bundle contents"
        else:
            verdict["reason"] = "integrity block does not re-fold to its content root"
    return verdict


def verify_export_bytes(raw: bytes, report_key: str) -> Dict[str, Any]:
    """Verify a bundle as it was written to disk/transferred.

    The on-the-wire form is JSON, so a verifier that re-parses gets the same
    verdict as :func:`verify_export_envelope`; this wrapper just makes the
    byte-level entry point explicit for third parties. A byte altered anywhere
    either breaks JSON parsing or fails the signature.
    """
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — malformed input is "unverified".
        return {
            "verified": False,
            "signature_valid": False,
            "integrity_consistent": False,
            "content_root_matches": False,
            "reason": f"bundle is not valid UTF-8 JSON: {type(exc).__name__}",
        }
    return verify_export_envelope(envelope, report_key)


def envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    """The canonical on-the-wire bytes for a signed envelope (download form)."""
    return canonical_bytes(dict(envelope))


def bundle_fingerprint(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    """Non-sensitive identifiers for audit/telemetry — never bundle content."""
    body = envelope.get("bundle") if isinstance(envelope, Mapping) else None
    body = body if isinstance(body, Mapping) else {}
    integrity = body.get("integrity")
    integrity = integrity if isinstance(integrity, Mapping) else {}
    signature = envelope.get("signature") if isinstance(envelope, Mapping) else None
    return {
        "scope": body.get("scope"),
        "run_id": body.get("run_id"),
        "opportunity_id": body.get("opportunity_id"),
        "finding_count": body.get("finding_count"),
        "record_count": integrity.get("record_count"),
        "content_root": integrity.get("content_root"),
        # A short prefix identifies the artifact without reproducing the MAC.
        "signature_prefix": (str(signature)[:16] if signature else None),
        "generated_at": body.get("generated_at"),
    }


__all__ = [
    "BUNDLE_VERSION",
    "INTEGRITY_ALGORITHM",
    "SIGNATURE_ALGORITHM",
    # Re-exported from usage_report: the canonical serialisation is part of the
    # verification contract, so a verifier imports it from here rather than
    # reaching into the usage-report module.
    "canonical_bytes",
    "SCOPE_FINDING",
    "SCOPE_REPORT",
    "VALID_SCOPES",
    "MAX_REPORT_FINDINGS",
    "EvidenceExportError",
    "record_hash",
    "build_integrity_block",
    "build_export_bundle",
    "generate_signed_export",
    "verify_export_envelope",
    "verify_export_bytes",
    "envelope_bytes",
    "bundle_fingerprint",
]
