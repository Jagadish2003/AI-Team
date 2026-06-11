"""Entity extraction overlays — ENT-1 (nCino Entity Extraction Hardening).

Customer-calibrated entity extraction rules layered on top of the generic
T3-S12-A entity extractor. Overlays declare customer-specific source field
names, object namespaces, and stage mappings without modifying the core pack.

One overlay per (org_id, connector_id). When no overlay is registered for an
org, the default T3-S12-A extraction runs unchanged.

Public surface (kept import-light to avoid circular imports — T1 requirement):
    base_overlay      — dataclasses: EntityExtractionOverlay, PersonFieldRule,
                        TeamFieldRule, ObjectRule
    overlay_registry  — register_overlay(), get_overlay(), startup sequence
    ncino_overlay     — common nCino patterns (person fields, object rules,
                        service-account patterns)
"""
from __future__ import annotations

from .base_overlay import (
    EntityExtractionOverlay,
    ObjectRule,
    PersonFieldRule,
    TeamFieldRule,
)
from .overlay_registry import (
    get_overlay,
    register_overlay,
    register_startup_overlays,
    unregister_overlay,
)

__all__ = [
    "EntityExtractionOverlay",
    "PersonFieldRule",
    "TeamFieldRule",
    "ObjectRule",
    "register_overlay",
    "get_overlay",
    "unregister_overlay",
    "register_startup_overlays",
]
