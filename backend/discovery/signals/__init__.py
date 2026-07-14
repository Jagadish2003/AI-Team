"""MSP Readiness Pack — cross-cloud signal schemas (Track A).

This package holds the normalised signal contracts the MSP pack's cloud-event
sources (AWS, Azure, and the Event History export bridge) emit against. The
first and load-bearing member is the Operational Event Schema (MSP-B0), the one
model every cloud source maps its provider-native payload onto so detectors
consume a single normalised shape.

See :mod:`discovery.signals.operational_event`.
"""

from .operational_event import (  # noqa: F401
    EVENT_CLASSES,
    RESOURCE_TYPES,
    SEVERITY_LEVELS,
    CommonSignal,
    OperationalEvent,
    ResourceRef,
    normalize_event_class,
    normalize_resource_type,
    normalize_severity,
)

__all__ = [
    "CommonSignal",
    "OperationalEvent",
    "ResourceRef",
    "RESOURCE_TYPES",
    "EVENT_CLASSES",
    "SEVERITY_LEVELS",
    "normalize_resource_type",
    "normalize_event_class",
    "normalize_severity",
]
