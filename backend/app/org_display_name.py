"""R17-D4 Addendum A / T12 — Dynamic Organisation Name: single display-name resolver.

Addendum A §2 ("Dynamic Organisation Name") makes the organisation name shown
across the UI (header, workspace labels, reports, License page) dynamic: once a
license key is installed it is read from the signed payload's ``org_name`` — the
license is the commercial source of truth for the deployment's identity, just as
it is for term (LIC-1) and scope (§1). Before any key is installed a neutral
default is shown — never a stale or placeholder customer name (AC16).

This module is the "one name, resolved once" of §5: a SINGLE resolver reads the
display name from the org's live-validated license, and every UI surface consumes
it (via ``GET /api/license/org-name``) rather than each carrying its own naming
logic. Because the read is live and side-effect-free, pasting a new key with a
different ``org_name`` updates every surface immediately, with no restart
(AC15) — the same no-restart posture as the LIC-1 renewal path.

Kept deliberately small and dependency-light — the §2 counterpart to
``license_limits`` (§1). The counting/entitlement logic (§1) and the naming logic
(§2) are the two halves of Addendum A and each live in their own module.
"""
from __future__ import annotations

import logging
from typing import Optional

from .license_runtime import get_current_license_status

logger = logging.getLogger(__name__)

# The neutral default shown before any key is installed (AC16). Deliberately
# generic — never a customer name — so a fresh, unlicensed install shows no stale
# or placeholder org identity anywhere. British "Organisation" matches the UI's
# existing spelling (e.g. the registration form's "Organisation name" label).
DEFAULT_ORG_DISPLAY_NAME = "Your Organisation"


def _display_name_from_result(result: dict) -> str:
    """Pure derivation of the display name from a license status result dict.

    Split out (like ``license_limits._build_limit_state``) so the resolution rule
    is unit-testable without a DB or a real license.

    A verified key is present exactly when the result carries a ``payload`` — the
    valid / grace / (past-grace) read-only states all return one. The no-key,
    invalid, and clock-rollback states carry no payload, so they resolve to the
    neutral default (AC16).

    When a payload is present the name is ``org_name`` (the Addendum A §2 display
    field), falling back to ``customer`` for keys issued before the addendum
    (which carry no ``org_name`` — showing the real customer name is correct, not
    stale), and finally to the neutral default if neither is a usable string.
    """
    payload = result.get("payload") or {}
    for candidate in (payload.get("org_name"), payload.get("customer")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return DEFAULT_ORG_DISPLAY_NAME


def resolve_org_display_name(org_id: Optional[str] = None) -> str:
    """The organisation display name for the org, read from its validated license.

    The single source consumed by every UI surface (Addendum A §2 / §5). Reads the
    org's live-validated license via ``license_runtime.get_current_license_status``
    (the side-effect-free path the run gate and limit checks also use), so a newly
    pasted key is reflected immediately with no restart (AC15).

    Returns the neutral ``DEFAULT_ORG_DISPLAY_NAME`` before a key is installed and
    for any non-verifiable state (AC16). Never raises — a display-name read must
    never break a page render, so any failure degrades to the neutral default.

    ``org_id`` defaults to the current request's org (resolved from the tenancy
    context by ``get_current_license_status``); pass it explicitly in tests or
    background contexts.
    """
    try:
        result = get_current_license_status(org_id=org_id)
    except Exception:  # pragma: no cover — defensive; a name read must never raise
        logger.exception(
            "org display name: license status read failed for org %s", org_id
        )
        return DEFAULT_ORG_DISPLAY_NAME
    return _display_name_from_result(result)
