"""
export_audit.py — Release 2.0-B1 T6: export-generation audit logging (AC6).

AC6: "Every export generation is an audit event naming user, scope, and time."

An export is the one operation in the product that puts signed, distributable
content *outside* the deployment — into an auditor's hands, a regulator's file,
or a board pack. So the question a security review asks is never "was a bundle
produced" but **who produced which artifact, when**. This module is the single
write point that answers it, so no export surface can answer it differently (or
forget to answer it at all).

Why a module rather than three inline ``log_event`` calls:

  * **One shape.** Every export event carries the same three mandatory parts —
    ``user_id`` (the audit row's actor column), ``scope`` (WHAT was exported),
    and ``timestamp`` (ISO-8601 UTC, in the payload as well as the row's own
    column) — plus a per-surface fingerprint identifying the exact artifact.
    :func:`build_export_audit_payload` is pure, so that shape is testable
    without a database.
  * **No unattributed export.** :func:`resolve_export_actor` derives the actor
    from the caller's bearer token through the SAME fail-closed resolver RBAC
    uses (``rbac._get_user_id_from_token`` verifies the signature before
    trusting a ``sub`` claim, so an audit record cannot be spoofed by a forged
    token). A token that yields nothing records the explicit
    :data:`UNATTRIBUTED_ACTOR` sentinel — mirroring tenancy's
    ``UNATTRIBUTED_ORG`` — never a silent ``NULL`` that reads as "no user was
    involved".
  * **A registry that fails the build.** :data:`EXPORT_AUDIT_SURFACES` names
    every export-generating route path and the export kind it records. A
    conformance test asserts that every registered route whose path looks like
    an export appears here, so a NEW export surface cannot ship unaudited by
    omission (the 2.0-D4 AC1 discipline, applied early to exports).

Recording is deliberately best-effort at the I/O boundary and strict at the
contract boundary: an unknown export kind is a programming error and raises,
while a failed audit/telemetry WRITE is logged loudly and swallowed — the bundle
has already been signed and handed over, and refusing to serve it because the
audit store hiccuped would neither un-export it nor help the auditor.
``middleware.audit.log_event`` is itself the fail-silent single write point for
``audit_log`` and emits its own ``audit.write_failed`` telemetry, so a persistence
failure stays observable rather than invisible.

Payload guard: identifiers, counts, and content HASHES only. Never bundle
content, never a whole signature (a prefix identifies an artifact; it does not
reproduce the MAC), never a credential.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

# ── export kinds ────────────────────────────────────────────────────────────

# 2.0-B1 T4 signed evidence bundles: one finding, or a whole run's report.
EXPORT_KIND_EVIDENCE_FINDING = "evidence_finding"
EXPORT_KIND_EVIDENCE_REPORT = "evidence_report"
# R-1.9.1-L2 signed usage report. Not a 2.0-B1 artifact, but it is an export
# generation on the same trust model (same signer, same distributable posture),
# and AC6 says EVERY export generation is an audit event — a signed artifact
# leaving the deployment with no record of who issued it is exactly the hole
# this ticket exists to close.
EXPORT_KIND_USAGE_REPORT = "usage_report"

VALID_EXPORT_KINDS = (
    EXPORT_KIND_EVIDENCE_FINDING,
    EXPORT_KIND_EVIDENCE_REPORT,
    EXPORT_KIND_USAGE_REPORT,
)

# Default scope label per kind. Callers pass the artifact's own scope where it
# has one (an evidence bundle's ``scope`` field is "finding"/"report"), so these
# are the fallback, not the source of truth.
_DEFAULT_SCOPE_BY_KIND: Dict[str, str] = {
    EXPORT_KIND_EVIDENCE_FINDING: "finding",
    EXPORT_KIND_EVIDENCE_REPORT: "report",
    EXPORT_KIND_USAGE_REPORT: "usage_report",
}

# Audit event type per kind. Imported from middleware.audit lazily (that module
# imports app.db at import time) so this module stays cheap to import.
_AUDIT_EVENT_BY_KIND: Dict[str, str] = {
    EXPORT_KIND_EVIDENCE_FINDING: "evidence_export_generated",
    EXPORT_KIND_EVIDENCE_REPORT: "evidence_export_generated",
    EXPORT_KIND_USAGE_REPORT: "usage_report_exported",
}

# Telemetry event type per kind, or None where the audit record is the whole
# story. ``record_event`` RAISES for an unregistered type, so a kind may only be
# mapped here once its payload schema is registered in app/telemetry.py.
_TELEMETRY_EVENT_BY_KIND: Dict[str, Optional[str]] = {
    EXPORT_KIND_EVIDENCE_FINDING: "export.evidence_generated",
    EXPORT_KIND_EVIDENCE_REPORT: "export.evidence_generated",
    # The usage report's own billing ledger already carries the commercial
    # trail; the audit event is what AC6 requires and what is missing.
    EXPORT_KIND_USAGE_REPORT: None,
}

# Route path → export kind. The conformance registry (see the module docstring).
EXPORT_AUDIT_SURFACES: Dict[str, str] = {
    "/api/runs/{run_id}/opportunities/{opp_id}/evidence-export":
        EXPORT_KIND_EVIDENCE_FINDING,
    "/api/runs/{run_id}/evidence-export": EXPORT_KIND_EVIDENCE_REPORT,
    "/api/usage/report": EXPORT_KIND_USAGE_REPORT,
}

# Recorded when the acting user cannot be resolved. An explicit sentinel, not
# NULL: "we do not know who" and "no user was involved" are different facts.
UNATTRIBUTED_ACTOR = "_unattributed"


# ── actor resolution ────────────────────────────────────────────────────────


def resolve_export_actor(token: Optional[str]) -> str:
    """Return the audit actor id for the bearer ``token``.

    Delegates to ``rbac._get_user_id_from_token`` — the repo's single
    token → user_id resolver, which verifies a JWT's signature before trusting
    its ``sub`` claim (so an audit record cannot be attributed to a spoofed
    user) and falls back to the token string itself for the static dev/test
    tokens, exactly as ``workspace_members`` rows are keyed.

    Never raises: a missing/unusable token yields :data:`UNATTRIBUTED_ACTOR` so
    the export is still recorded, visibly unattributed.
    """
    if not token:
        return UNATTRIBUTED_ACTOR
    try:
        from .rbac import _get_user_id_from_token

        actor = _get_user_id_from_token(token)
    except Exception as exc:  # noqa: BLE001 — attribution must not break an export.
        logger.warning("export_audit: actor resolution failed: %s", exc)
        return UNATTRIBUTED_ACTOR
    actor = str(actor or "").strip()
    return actor or UNATTRIBUTED_ACTOR


# ── payload ─────────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def build_export_audit_payload(
    export_kind: str,
    *,
    actor: Optional[str],
    scope: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one export-generation audit payload (pure — no I/O).

    The three AC6 parts are guaranteed present and non-empty:

      * ``user_id``   — the actor (:data:`UNATTRIBUTED_ACTOR` when unknown).
                        ``log_event`` POPS this and writes it to the audit row's
                        own ``user_id`` column — the canonical actor field every
                        other audit event uses — so it does not appear inside the
                        stored JSON payload.
      * ``scope``     — WHAT was exported ("finding" / "report" /
                        "usage_report"), plus the ``details`` identifying the
                        exact artifact.
      * ``timestamp`` — ISO-8601 UTC. ``log_event`` stamps the row's own
                        ``timestamp`` column as well; this one is inside the
                        payload so the export event carries its time even when
                        the payload travels on its own.

    ``details`` supplies the per-surface fingerprint (ids, counts, content root,
    signature prefix). Its keys never override the three mandatory parts, so a
    caller cannot accidentally blank out the actor by passing a stray ``user_id``.

    Raises ``ValueError`` for an unknown ``export_kind``: every call site passes
    a module constant, so an unknown kind is a programming error that must fail
    loudly rather than write an unclassifiable audit record.
    """
    if export_kind not in VALID_EXPORT_KINDS:
        raise ValueError(
            f"unknown export kind {export_kind!r}; expected one of "
            f"{list(VALID_EXPORT_KINDS)}"
        )

    payload: Dict[str, Any] = {}
    for key, value in (details or {}).items():
        if key in ("user_id", "timestamp", "export_kind"):
            continue
        payload[key] = value

    resolved_scope = str(scope or "").strip() or _DEFAULT_SCOPE_BY_KIND[export_kind]
    payload["export_kind"] = export_kind
    payload["scope"] = resolved_scope
    payload["user_id"] = str(actor or "").strip() or UNATTRIBUTED_ACTOR
    payload["timestamp"] = timestamp or _utc_now_iso()
    return payload


# ── recording ───────────────────────────────────────────────────────────────


def record_export_generated(
    export_kind: str,
    *,
    org_id: str,
    actor: Optional[str],
    scope: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one export generation as an audit event (AC6), plus telemetry
    where the kind registers a telemetry type.

    Returns the audit payload that was written, so a caller (and a test) can see
    exactly what was recorded. Raises only for an unknown ``export_kind``; a
    failed audit or telemetry WRITE is logged and swallowed, because the artifact
    has already been produced and denying the caller their bundle would not
    un-export it.
    """
    payload = build_export_audit_payload(
        export_kind, actor=actor, scope=scope, details=details, timestamp=timestamp
    )

    audit_event = _AUDIT_EVENT_BY_KIND[export_kind]
    try:
        from .middleware.audit import (
            EVIDENCE_EXPORT_GENERATED,
            USAGE_REPORT_EXPORTED,
            log_event,
        )

        # 2.0-D4 T1's audit-conformance sweep resolves log_event's event-type
        # argument STATICALLY (an AST walk over every call site, not at runtime)
        # so it can prove every emitted type is registered before it ever runs.
        # A single call driven by the ``_AUDIT_EVENT_BY_KIND`` dict lookup above
        # is invisible to that walk — it sees a local variable, not a literal —
        # so each kind calls log_event with its OWN imported constant. This is
        # still the one write point (one function, same behaviour); only the
        # call site is unrolled so the sweep can see what it proves.
        if export_kind in (EXPORT_KIND_EVIDENCE_FINDING, EXPORT_KIND_EVIDENCE_REPORT):
            log_event(EVIDENCE_EXPORT_GENERATED, org_id=org_id, **payload)
        else:
            log_event(USAGE_REPORT_EXPORTED, org_id=org_id, **payload)
    except Exception as exc:  # noqa: BLE001 — log_event is itself non-raising.
        logger.warning(
            "export_audit: audit write failed for %s (%s): %s",
            audit_event, export_kind, exc,
        )

    telemetry_event = _TELEMETRY_EVENT_BY_KIND.get(export_kind)
    if telemetry_event:
        try:
            from .telemetry import record_event

            # Telemetry carries the fingerprint + scope; the actor is audit-trail
            # data and deliberately stays out of the analytics stream.
            record_event(
                telemetry_event,
                {
                    "org_id": org_id,
                    **{
                        k: v for k, v in payload.items()
                        if k not in ("user_id", "timestamp", "export_kind")
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 — telemetry is fire-and-forget.
            logger.debug(
                "export_audit: telemetry failed for %s: %s", telemetry_event, exc
            )

    return payload


__all__ = [
    "EXPORT_KIND_EVIDENCE_FINDING",
    "EXPORT_KIND_EVIDENCE_REPORT",
    "EXPORT_KIND_USAGE_REPORT",
    "VALID_EXPORT_KINDS",
    "EXPORT_AUDIT_SURFACES",
    "UNATTRIBUTED_ACTOR",
    "resolve_export_actor",
    "build_export_audit_payload",
    "record_export_generated",
]
