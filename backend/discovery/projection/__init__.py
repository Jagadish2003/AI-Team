"""2.0-A1 intervention projection — per-opportunity projection model.

A projection is NOT a promise.  It is a *direction* and a *magnitude band* on
specific *measured* signals, with the basis shown and the band widened when the
evidence is thin.  2.0-A2 later narrows these bands from real outcomes.

Public API (import from this package, not the submodules):

    from discovery.projection import build_projection, project_opportunities

`signal_registry` maps each detector to the real measured-signal field names it
already emits (see each detector's ``SIGNAL_METRICS``) — the projection never
invents a signal name.  `model` computes direction, band, horizon, the replaced
manual step, and the movement signal deterministically.
"""

from .model import (
    PROJECTION_SCHEMA_VERSION,
    DIRECTION_IMPROVES,
    DIRECTION_NO_MATERIAL_CHANGE,
    HORIZON_30,
    HORIZON_60,
    HORIZON_90,
    MagnitudeBand,
    Projection,
    ProjectedSignal,
    build_projection,
    project_opportunities,
)
from .signal_registry import (
    SIGNAL_CONCEPTS,
    CONCEPT_QUEUE_VOLUME,
    CONCEPT_AGEING,
    CONCEPT_RECURRENCE,
    CONCEPT_TIME_TO_RESOLVE,
    CONCEPT_REASSIGNMENT,
    DetectorSignalProfile,
    get_detector_profile,
    known_detector_ids,
)

__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "DIRECTION_IMPROVES",
    "DIRECTION_NO_MATERIAL_CHANGE",
    "HORIZON_30",
    "HORIZON_60",
    "HORIZON_90",
    "MagnitudeBand",
    "Projection",
    "ProjectedSignal",
    "build_projection",
    "project_opportunities",
    "SIGNAL_CONCEPTS",
    "CONCEPT_QUEUE_VOLUME",
    "CONCEPT_AGEING",
    "CONCEPT_RECURRENCE",
    "CONCEPT_TIME_TO_RESOLVE",
    "CONCEPT_REASSIGNMENT",
    "DetectorSignalProfile",
    "get_detector_profile",
    "known_detector_ids",
]
