"""Overlay registry — ENT-1 Task T2.

Stores and retrieves active entity-extraction overlays, keyed by
(org_id, connector_id) so one customer's rules never leak into another
customer's workspace.

When no overlay exists for an (org_id, connector_id), get_overlay() returns
None and the default T3-S12-A extraction runs unchanged. Returning None is the
contract that keeps default extraction safe for every org without an overlay.

Registration happens at startup / deployment configuration time — never at
query time. See register_startup_overlays().
"""
from __future__ import annotations

import logging
import re
from threading import RLock
from typing import Dict, Optional, Tuple

from .base_overlay import EntityExtractionOverlay

logger = logging.getLogger(__name__)

# Active overlays, keyed by (org_id, connector_id).
# Module-level so a registration is immediately visible to the next extraction
# in the same process (AC7: no server restart needed between registration and
# the first discovery run).
OVERLAY_REGISTRY: Dict[Tuple[str, str], EntityExtractionOverlay] = {}
_REGISTRY_LOCK = RLock()


def _validate_overlay(overlay: EntityExtractionOverlay) -> None:
    for pattern in overlay.service_account_patterns or []:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                "Invalid service_account_patterns regex "
                f"{pattern!r} for org={overlay.org_id} "
                f"connector={overlay.connector_id}: {exc}"
            ) from exc


def register_overlay(overlay: EntityExtractionOverlay) -> None:
    """Register an overlay for its (org_id, connector_id).

    Idempotent per key: re-registering the same key replaces the previous
    overlay (supports deploying a new overlay *version* for a customer whose
    Salesforce schema changed). Scoped strictly to the overlay's own org and
    connector — never affects another org.
    """
    if not isinstance(overlay, EntityExtractionOverlay):
        raise TypeError(
            f"register_overlay expects an EntityExtractionOverlay, got {type(overlay)!r}"
        )
    _validate_overlay(overlay)
    key = (overlay.org_id, overlay.connector_id)
    with _REGISTRY_LOCK:
        OVERLAY_REGISTRY[key] = overlay
    logger.info(
        "entity overlay registered: org=%s connector=%s version=%s",
        overlay.org_id,
        overlay.connector_id,
        overlay.version,
    )


def get_overlay(org_id: str, connector_id: str) -> Optional[EntityExtractionOverlay]:
    """Return the active overlay for (org_id, connector_id), or None.

    None means "no customer overlay registered" — the caller must fall back to
    the default T3-S12-A extraction. This keeps default extraction safe for
    every org that has not been onboarded with an overlay yet.
    """
    with _REGISTRY_LOCK:
        return OVERLAY_REGISTRY.get((org_id, connector_id))


def unregister_overlay(org_id: str, connector_id: str) -> None:
    """Remove an overlay registration. Safe to call when none is registered.

    Primarily used by tests for isolation and by deployment tooling when an
    overlay is retired.
    """
    with _REGISTRY_LOCK:
        OVERLAY_REGISTRY.pop((org_id, connector_id), None)


def registered_keys() -> list[Tuple[str, str]]:
    """Return the list of (org_id, connector_id) keys currently registered."""
    with _REGISTRY_LOCK:
        return list(OVERLAY_REGISTRY.keys())


def register_startup_overlay(overlay: EntityExtractionOverlay) -> bool:
    """Register one startup overlay and keep startup non-blocking.

    Deployment-specific startup code should call this helper for each customer
    overlay so one broken overlay logs a clear warning and does not prevent the
    remaining overlays from registering.
    """
    try:
        register_overlay(overlay)
        return True
    except Exception as exc:
        logger.warning(
            "entity overlay startup registration failed: org=%s connector=%s error=%s",
            getattr(overlay, "org_id", "<unknown>"),
            getattr(overlay, "connector_id", "<unknown>"),
            exc,
        )
        return False


def register_startup_overlays() -> None:
    """Startup registration sequence — the single place to wire customer overlays.

    Called once from the FastAPI lifespan (app startup). This is where an
    implementation engineer registers known customer overlays so they are active
    before the first discovery run, e.g.::

        from app.entity_overlays.ncino_overlay import build_ncino_overlay
        register_overlay(build_ncino_overlay(org_id="city-national"))

    By design this ships as a no-op: the core app hardcodes *no* customer
    overlays (the hard rule from ENT-1 — the core pack is never modified for a
    customer). Customer overlays are added here at deployment time, kept in
    version control, and reviewed per customer onboarding.

    Never raises — a misconfigured overlay must not block app startup.
    """
    try:
        # Intentionally empty: no customer overlays are hardcoded into the core.
        # Deployment-specific overlay registrations are added here.
        with _REGISTRY_LOCK:
            registered = len(OVERLAY_REGISTRY)
        logger.info("entity overlay startup: %d overlays registered", registered)
    except Exception as exc:  # pragma: no cover — defensive; startup must not fail
        logger.warning("entity overlay startup sequence failed (non-blocking): %s", exc)
