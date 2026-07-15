"""MSP Readiness Pack — cross-cloud signal schemas (Track A).

This package holds the normalised signal contracts the MSP pack's cloud-event
sources (AWS, Azure, and the Event History export bridge) emit against. The
first and load-bearing member is the Operational Event Schema (MSP-B0), the one
model every cloud source maps its provider-native payload onto so detectors
consume a single normalised shape.

See :mod:`discovery.signals.operational_event`.
"""

from .event_signature import (  # noqa: F401
    EVENT_SIGNATURE_VERSION,
    PROVIDER_FAMILIES,
    compute_event_signature,
    provider_family,
    signature_components,
)
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
from .evidence_store import (  # noqa: F401
    InMemoryRawEventStore,
    OrgScopeError,
    RawEventStore,
    map_and_store,
    resolve_raw_event,
    store_raw_event,
)
from .reference_mappers import (  # noqa: F401
    MAPPERS,
    aws_resource_type_from_arn,
    azure_resource_type_from_id,
    map_azure_activity_log,
    map_azure_monitor,
    map_cloudtrail,
    map_cloudwatch,
    map_eventbridge,
)
from .resource_graph import (  # noqa: F401
    CLOUD_RESOURCE_ENTITY_TYPE,
    create_resource_entities,
)
from .ops_stream import (  # noqa: F401
    DEFAULT_ACTIVE_PERIOD_SECONDS,
    ActiveSignal,
    Admission,
    OpsEventStream,
    fold_events,
)
from .budget import (  # noqa: F401
    DEFAULT_RUN_EVENT_BUDGET,
    BudgetReport,
    RunBudget,
)
from .ops_calibration import (  # noqa: F401
    B8_MEASUREMENTS,
    CALIBRATED_CORRELATION_WINDOWS,
    CALIBRATED_DEFAULT_FLOOR,
    CALIBRATED_DEFAULT_WINDOW_SECONDS,
    CALIBRATED_NOISE_FLOORS,
    CALIBRATED_RUN_EVENT_BUDGET,
    calibration_summary,
)
from .aggregation import (  # noqa: F401
    DEFAULT_EVIDENCE_SAMPLE_SIZE,
    HIGH_CARDINALITY_CLASSES,
    AggregateSignal,
    aggregate_active_signal,
    aggregate_events,
    roll_up,
)
from .noise_floor import (  # noqa: F401
    DEFAULT_FLOOR,
    DEFAULT_NOISE_FLOORS,
    NoiseFloorPolicy,
    SuppressionReport,
    apply_noise_floors,
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
    # AT-636 — event signature
    "compute_event_signature",
    "signature_components",
    "provider_family",
    "PROVIDER_FAMILIES",
    "EVENT_SIGNATURE_VERSION",
    # AT-637 — reference mappers
    "map_cloudwatch",
    "map_eventbridge",
    "map_cloudtrail",
    "map_azure_monitor",
    "map_azure_activity_log",
    "aws_resource_type_from_arn",
    "azure_resource_type_from_id",
    "MAPPERS",
    # AT-638 — raw-payload storage + evidence resolution
    "RawEventStore",
    "InMemoryRawEventStore",
    "OrgScopeError",
    "store_raw_event",
    "resolve_raw_event",
    "map_and_store",
    # AT-639 — resource entities into the graph
    "create_resource_entities",
    "CLOUD_RESOURCE_ENTITY_TYPE",
    # AT-669 (MSP-B7 T1) — dedup at admission (active-signal folding)
    "OpsEventStream",
    "ActiveSignal",
    "Admission",
    "fold_events",
    "DEFAULT_ACTIVE_PERIOD_SECONDS",
    # AT-670 (MSP-B7 T2) — aggregation roll-ups for high-cardinality classes
    "AggregateSignal",
    "roll_up",
    "aggregate_active_signal",
    "aggregate_events",
    "HIGH_CARDINALITY_CLASSES",
    "DEFAULT_EVIDENCE_SAMPLE_SIZE",
    # AT-671 (MSP-B7 T3) — noise floors per event class
    "NoiseFloorPolicy",
    "SuppressionReport",
    "apply_noise_floors",
    "DEFAULT_NOISE_FLOORS",
    "DEFAULT_FLOOR",
    # AT-672 (MSP-B7 T4) — per-run event-volume budgets
    "BudgetReport",
    "RunBudget",
    "DEFAULT_RUN_EVENT_BUDGET",
    # AT-674 (MSP-B7 T6) — calibration from B8's month-scale sample
    "B8_MEASUREMENTS",
    "CALIBRATED_RUN_EVENT_BUDGET",
    "CALIBRATED_NOISE_FLOORS",
    "CALIBRATED_DEFAULT_FLOOR",
    "CALIBRATED_CORRELATION_WINDOWS",
    "CALIBRATED_DEFAULT_WINDOW_SECONDS",
    "calibration_summary",
]
