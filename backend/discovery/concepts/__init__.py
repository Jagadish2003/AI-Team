"""2.0-B4 — the normalised concept set (T1).

MSP-B0 proved the principle on cloud events: one normalised shape, every provider
maps onto it, detectors stop branching on provider. This package widens that pattern
to the source families AgentIQ ingests, so a detector — and a partner-authored pack
(2.0-C3) — composes against a concept rather than a connector's field names.

Three modules, three questions:

* :mod:`~discovery.concepts.model` — *what* the concepts are. Six profiles of
  ``CommonSignal`` (work item, actor group, artifact, state transition, approval,
  assignment) plus ``EntityReference``, the shared reference value type. Closed
  vocabularies, validated at construction.
* :mod:`~discovery.concepts.contracts` — *how* a source maps onto them, as
  versioned data rather than prose. ``CONCEPT_SET_VERSION`` plus a per-concept
  contract version, with the bump rules stated in the module.
* :mod:`~discovery.concepts.conformance` — *who* conforms. One declaration per
  shipped connector, with recorded concept-level and field-level gaps.

2.0-B4 T2 adds the mapping itself:

* :mod:`~discovery.concepts.mappers` — the per-connector mappers and the registry that
  makes a ``supported`` conformance claim resolve to real code.
* :mod:`~discovery.concepts.gaps` — the declared-gap surface (AC5), inverted
  concept-first for pack authors, plus ``assert_no_approximation``.

Documented in ``docs/normalised_concepts.md``.
"""

from __future__ import annotations

from .contracts import (
    BREAKING_CHANGE_RULES,
    CONCEPT_SET_VERSION,
    CONTRACTS,
    FieldContract,
    MappingContract,
    contract_summary,
    get_contract,
    vocabulary,
)
from .conformance import (
    CONFORMANCE,
    FIELD_GAP_KINDS,
    GAP_ABSENT,
    GAP_PARTIAL,
    STATUS_DECLARED,
    STATUS_GAP,
    STATUS_NOT_APPLICABLE,
    STATUS_SUPPORTED,
    STATUSES,
    ConceptConformance,
    ConformanceError,
    ConnectorConformance,
    FieldGap,
    conformance_summary,
    connectors_supporting,
    declared_gaps,
    get_conformance,
    stale_declarations,
)
from .gaps import (
    ApproximationError,
    assert_no_approximation,
    concept_gap_report,
    concepts_usable_by,
    connector_gap_report,
    connectors_for_detector,
    field_gaps_for,
    gap_summary,
    unpopulated_fields,
)
from .mappers import (
    MAPPERS,
    ConceptMapper,
    MapperError,
    get_mapper,
    mapped_concepts,
    registry_summary,
    resolve_mapper,
)
from .concept_detectors import (
    DEFAULT_MIN_OPEN,
    detect_open_work_item_backlog,
)
from .conformance_fixtures import (
    CONFORMANCE_FIXTURE_DIR,
    available_fixture_ids,
    load_all_fixtures,
    load_fixture,
)
from .portable_detectors import (
    detect_approval_bottleneck,
    detect_permission_bottleneck,
)
from .sdk_vocabulary import (
    SDK_HANDOFF,
    STABILITY_CONTRACT,
    VOCABULARY_VERSION,
    concepts_available_from,
    publish_vocabulary,
    sources_for_required_concepts,
    unsupported_requirements,
    vocabulary_digest,
)
from .model import (
    ACTOR_GROUP_TYPES,
    APPROVAL_DECISIONS,
    APPROVAL_TYPES,
    ARTIFACT_TYPES,
    ASSIGNMENT_TYPES,
    CONCEPT_ACTOR_GROUP,
    CONCEPT_APPROVAL,
    CONCEPT_ARTIFACT,
    CONCEPT_ASSIGNMENT,
    CONCEPT_CLASSES,
    CONCEPT_ENTITY_REFERENCE,
    CONCEPT_SET,
    CONCEPT_STATE_TRANSITION,
    CONCEPT_WORK_ITEM,
    CONTENT_TYPES,
    ENTITY_REFERENCE_TYPES,
    PRIORITY_LEVELS,
    STATUS_CATEGORIES,
    TRANSITION_TYPES,
    WORK_ITEM_TYPES,
    ActorGroup,
    Approval,
    Artifact,
    Assignment,
    ConceptSignal,
    EntityReference,
    StateTransition,
    WorkItem,
)

__all__ = [
    # the set
    "CONCEPT_SET",
    "CONCEPT_WORK_ITEM",
    "CONCEPT_ACTOR_GROUP",
    "CONCEPT_ARTIFACT",
    "CONCEPT_STATE_TRANSITION",
    "CONCEPT_APPROVAL",
    "CONCEPT_ASSIGNMENT",
    "CONCEPT_ENTITY_REFERENCE",
    "CONCEPT_CLASSES",
    # profiles + reference type
    "ConceptSignal",
    "EntityReference",
    "WorkItem",
    "ActorGroup",
    "Artifact",
    "StateTransition",
    "Approval",
    "Assignment",
    # vocabularies
    "WORK_ITEM_TYPES",
    "STATUS_CATEGORIES",
    "PRIORITY_LEVELS",
    "ACTOR_GROUP_TYPES",
    "ARTIFACT_TYPES",
    "CONTENT_TYPES",
    "TRANSITION_TYPES",
    "APPROVAL_DECISIONS",
    "APPROVAL_TYPES",
    "ASSIGNMENT_TYPES",
    "ENTITY_REFERENCE_TYPES",
    # contracts
    "CONCEPT_SET_VERSION",
    "BREAKING_CHANGE_RULES",
    "CONTRACTS",
    "FieldContract",
    "MappingContract",
    "get_contract",
    "vocabulary",
    "contract_summary",
    # conformance
    "STATUSES",
    "STATUS_SUPPORTED",
    "STATUS_DECLARED",
    "STATUS_GAP",
    "STATUS_NOT_APPLICABLE",
    "ConformanceError",
    "ConceptConformance",
    "ConnectorConformance",
    "CONFORMANCE",
    "get_conformance",
    "connectors_supporting",
    "declared_gaps",
    "stale_declarations",
    "conformance_summary",
    "FieldGap",
    "GAP_ABSENT",
    "GAP_PARTIAL",
    "FIELD_GAP_KINDS",
    # mappers (T2)
    "MAPPERS",
    "ConceptMapper",
    "MapperError",
    "get_mapper",
    "mapped_concepts",
    "resolve_mapper",
    "registry_summary",
    # gaps (T2 / AC5)
    "ApproximationError",
    "assert_no_approximation",
    "concept_gap_report",
    "connector_gap_report",
    "connectors_for_detector",
    "concepts_usable_by",
    "field_gaps_for",
    "unpopulated_fields",
    "gap_summary",
    # portable + concept-native detectors (T3 / T4)
    "detect_approval_bottleneck",
    "detect_permission_bottleneck",
    "detect_open_work_item_backlog",
    "DEFAULT_MIN_OPEN",
    # conformance fixtures (T5)
    "CONFORMANCE_FIXTURE_DIR",
    "available_fixture_ids",
    "load_fixture",
    "load_all_fixtures",
    # the published partner vocabulary (T6)
    "VOCABULARY_VERSION",
    "SDK_HANDOFF",
    "STABILITY_CONTRACT",
    "publish_vocabulary",
    "vocabulary_digest",
    "concepts_available_from",
    "sources_for_required_concepts",
    "unsupported_requirements",
]
