"""2.0-A1 intervention projection — per-opportunity projection model.

A projection is NOT a promise.  It is a *direction* and a *magnitude band* on
specific *measured* signals, with the basis shown and the band widened when the
evidence is thin.  2.0-A2 later narrows these bands from real outcomes.

Public API (import from this package, not the submodules):

    from discovery.projection import build_projection, project_opportunities

`signal_registry` maps each detector to the real measured-signal field names it
already emits (see each detector's ``SIGNAL_METRICS``) — the projection never
invents a signal name.  `model` computes direction, band, horizon, the replaced
manual step, and the movement signal deterministically.  `band_width` owns HOW
WIDE the band is — a deterministic function of four evidence inputs (sample
size, recurrence stability, corroboration status, confidence cap status) and
never a hand-set number — plus the projection-strength scalar and the AC4
ordering rule that keeps a capped finding from out-ranking a corroborated one.
"""

from .band_width import (
    BAND_WIDTH_MODEL_VERSION,
    AXIS_CONFIDENCE_CAP,
    AXIS_CORROBORATION,
    AXIS_RECURRENCE_STABILITY,
    AXIS_SAMPLE_SIZE,
    AXIS_WEIGHTS,
    CAPPED_STRENGTH_CEILING,
    CAPPED_STRENGTH_LABEL,
    BandWidth,
    BandWidthDriver,
    BandWidthInputs,
    band_width_inputs_from_opportunity,
    classify_confidence_cap,
    classify_corroboration_status,
    classify_recurrence_stability,
    classify_sample_tier,
    compute_band_width,
    demote_capped_projections,
    order_by_projection_strength,
    projection_is_capped,
    projection_rank_key,
    projection_strength_of,
)
from .provenance import (
    PROVENANCE_KEY,
    PROVENANCE_SCHEMA_VERSION,
    REQUIRED_PROVENANCE_FIELDS,
    build_provenance,
    get_provenance,
    is_storable,
    missing_provenance_fields,
    projection_core,
    stamp_projection,
)
from .recommendation import (
    RECOMMENDATION_SCHEMA_VERSION,
    REQUIRED_PARTS,
    Recommendation,
    RecommendationPart,
    build_recommendation,
)
from .vocabulary import (
    VOCABULARY_VERSION,
    CATEGORY_GUARANTEE,
    CATEGORY_POINT_ESTIMATE,
    ProhibitedVocabularyError,
    VocabularyViolation,
    assert_clean,
    contains_prohibited,
    sanitize_bullets,
    sanitize_text,
    scan_payload,
    scan_text,
)
from .model import (
    PROJECTION_SCHEMA_VERSION,
    DIRECTION_IMPROVES,
    DIRECTION_NO_MATERIAL_CHANGE,
    HORIZON_30,
    HORIZON_60,
    HORIZON_90,
    MagnitudeBand,
    Projection,
    ProjectionAssumption,
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
    "BAND_WIDTH_MODEL_VERSION",
    "RECOMMENDATION_SCHEMA_VERSION",
    "VOCABULARY_VERSION",
    "PROVENANCE_SCHEMA_VERSION",
    "PROVENANCE_KEY",
    "REQUIRED_PROVENANCE_FIELDS",
    "build_provenance",
    "get_provenance",
    "is_storable",
    "missing_provenance_fields",
    "projection_core",
    "stamp_projection",
    "REQUIRED_PARTS",
    "CATEGORY_GUARANTEE",
    "CATEGORY_POINT_ESTIMATE",
    "ProhibitedVocabularyError",
    "Recommendation",
    "RecommendationPart",
    "VocabularyViolation",
    "assert_clean",
    "build_recommendation",
    "contains_prohibited",
    "sanitize_bullets",
    "sanitize_text",
    "scan_payload",
    "scan_text",
    "AXIS_SAMPLE_SIZE",
    "AXIS_RECURRENCE_STABILITY",
    "AXIS_CORROBORATION",
    "AXIS_CONFIDENCE_CAP",
    "AXIS_WEIGHTS",
    "CAPPED_STRENGTH_CEILING",
    "CAPPED_STRENGTH_LABEL",
    "BandWidth",
    "BandWidthDriver",
    "BandWidthInputs",
    "band_width_inputs_from_opportunity",
    "classify_confidence_cap",
    "classify_corroboration_status",
    "classify_recurrence_stability",
    "classify_sample_tier",
    "compute_band_width",
    "demote_capped_projections",
    "order_by_projection_strength",
    "projection_is_capped",
    "projection_rank_key",
    "projection_strength_of",
    "DIRECTION_IMPROVES",
    "DIRECTION_NO_MATERIAL_CHANGE",
    "HORIZON_30",
    "HORIZON_60",
    "HORIZON_90",
    "MagnitudeBand",
    "ProjectionAssumption",
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
