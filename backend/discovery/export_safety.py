"""
export_safety.py — Release 2.0-B1 T5 (AC5): the discovery-side export bridge.

``app.export_guard`` owns the two disciplines every export path must hold
(secret redaction, then the 1.9 SecOps aggregation floor). The discovery package
runs as standalone CLIs as well as inside the app, so it cannot assume the ``app``
package is importable. This module is the ONE tolerant bridge — every
discovery-side export CLI calls :func:`guard_exported_payload` rather than
repeating the import shim (and risking one of them getting it subtly wrong).

A missing ``app`` package degrades to a no-op that is logged at WARNING, never a
silent pass: an export that skipped the guard must be visible in the output.
A floor breach propagates, so the CLI fails and writes nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["ExportGuardViolation", "guard_exported_payload"]


def _load_guard() -> Optional[Tuple[Any, type]]:
    """Return ``(guard_export_payload, ExportGuardViolation)``, or None.

    Import order is load-bearing. This repo is importable as both ``app.*`` and
    ``backend.app.*``, and importing the guard under BOTH names would create two
    distinct module objects — and therefore two distinct
    ``ExportGuardViolation`` CLASSES, so a caller's ``except`` would silently
    fail to catch the one that was actually raised. ``app.*`` is tried first
    because that is how the app itself imports the guard (``evidence_export``
    uses a relative import), so discovery-side and app-side callers converge on
    one class.
    """
    try:
        from app.export_guard import ExportGuardViolation, guard_export_payload

        return guard_export_payload, ExportGuardViolation
    except ModuleNotFoundError:
        pass
    try:
        from backend.app.export_guard import (  # type: ignore[no-redef]
            ExportGuardViolation,
            guard_export_payload,
        )

        return guard_export_payload, ExportGuardViolation
    except ModuleNotFoundError:
        return None


_loaded = _load_guard()

# Re-exported so a discovery-side caller catches a STABLE symbol
# (``from discovery.export_safety import ExportGuardViolation``) rather than
# guessing which underlying module supplied the class. When the app package is
# unavailable this is a local stand-in so ``except`` clauses still parse.
if _loaded is not None:
    _guard_export_payload, ExportGuardViolation = _loaded
else:  # pragma: no cover - only in a checkout without the app package
    _guard_export_payload = None

    class ExportGuardViolation(Exception):  # type: ignore[no-redef]
        """Stand-in used when app.export_guard cannot be imported."""


def guard_exported_payload(payload: Any, *, where: str) -> Any:
    """Return ``payload`` redacted and floor-checked, ready to write/serialise.

    Raises :class:`ExportGuardViolation` (re-exported from this module) when the
    content breaches the aggregation floor — the caller must let that fail the
    export rather than writing a partial or unguarded artifact.
    """
    if _guard_export_payload is None:
        logger.warning(
            "export guard unavailable (app package not importable) — emitting %s "
            "WITHOUT redaction/aggregation-floor enforcement",
            where,
        )
        return payload

    guarded = _guard_export_payload(payload, where=where)
    if guarded.redacted_pattern_types:
        # Pattern TYPE names only — never a redacted value.
        logger.info(
            "Redacted secret pattern types from %s before writing: %s",
            where, guarded.redacted_pattern_types,
        )
    return guarded.payload
