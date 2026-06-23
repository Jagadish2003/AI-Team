"""Base overlay dataclasses — ENT-1 Task T1.

These dataclasses declare *customer-calibrated* entity extraction rules for one
connector. They are pure data — no imports from app/ or database/ — so they can
never introduce a circular import into the entity extractor (a T1 requirement).

An overlay maps customer-specific source field names to AgentIQ entity
extraction patterns. The generic T3-S12-A extractor stays unchanged; overlays
capture the per-customer variation (field naming, object namespace, which
LLC_BI__* extensions are active) without polluting the core pack.

One overlay per (org_id, connector_id). See overlay_registry.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

# Resolution sources for a person/team field rule.
#   'id'   → the field carries a stable source system ID (e.g. a Salesforce
#            OwnerId). Resolved with confidence 1.0 — IDs beat name matching.
#   'name' → the field carries only a display name. Resolved heuristically with
#            the default name-based confidence (0.8) in entity_resolution.
_VALID_RESOLUTION_SOURCES = frozenset({"id", "name"})


@dataclass
class PersonFieldRule:
    """How to extract a Person entity from a customer-specific source field.

    Example: a rule may say that ``LLC_BI__Loan__c.OwnerId`` or
    ``LLC_BI__Loan__c.LLC_BI__Loan_Officer__c`` should be treated as a Person,
    and whether to resolve by source ID (confidence 1.0) or display name (0.8).
    """

    object_api_name: str            # e.g. 'LLC_BI__Loan__c'
    field_api_name: str             # e.g. 'OwnerId' or 'LLC_BI__Loan_Officer__c'
    resolution_source: str = "id"   # 'id' (high confidence) | 'name' (heuristic)
    label: str = ""                 # human label: 'Loan Officer' | 'Relationship Manager'

    def __post_init__(self) -> None:
        if not self.object_api_name or not str(self.object_api_name).strip():
            raise ValueError("PersonFieldRule.object_api_name is required")
        if not self.field_api_name or not str(self.field_api_name).strip():
            raise ValueError("PersonFieldRule.field_api_name is required")
        if self.resolution_source not in _VALID_RESOLUTION_SOURCES:
            raise ValueError(
                f"PersonFieldRule.resolution_source must be one of "
                f"{sorted(_VALID_RESOLUTION_SOURCES)}, got {self.resolution_source!r}"
            )


@dataclass
class TeamFieldRule:
    """How to extract a Team entity from a customer-specific source field.

    Example: a credit-team or relationship-team lookup field on a loan object.
    """

    object_api_name: str            # e.g. 'LLC_BI__Loan__c'
    field_api_name: str             # e.g. 'LLC_BI__Credit_Team__c'
    resolution_source: str = "id"   # 'id' | 'name'
    label: str = ""                 # human label: 'Credit Team' | 'Underwriting Team'

    def __post_init__(self) -> None:
        if not self.object_api_name or not str(self.object_api_name).strip():
            raise ValueError("TeamFieldRule.object_api_name is required")
        if not self.field_api_name or not str(self.field_api_name).strip():
            raise ValueError("TeamFieldRule.field_api_name is required")
        if self.resolution_source not in _VALID_RESOLUTION_SOURCES:
            raise ValueError(
                f"TeamFieldRule.resolution_source must be one of "
                f"{sorted(_VALID_RESOLUTION_SOURCES)}, got {self.resolution_source!r}"
            )


@dataclass
class ObjectRule:
    """How to extract an Object/Process entity from a customer-specific object.

    Identifies an important business object (loan, covenant, checklist item,
    spreading record, approval record) so AgentIQ understands which objects are
    involved in a detected opportunity.
    """

    object_api_name: str            # e.g. 'LLC_BI__Covenant__c'
    entity_type: str = "object"     # 'object' | 'process'
    name_field: str = "Name"        # which field to use as display_name
    record_type: str = ""           # human label: 'Covenant' | 'Loan' | 'Application'

    def __post_init__(self) -> None:
        if not self.object_api_name or not str(self.object_api_name).strip():
            raise ValueError("ObjectRule.object_api_name is required")
        if not self.name_field or not str(self.name_field).strip():
            raise ValueError("ObjectRule.name_field is required")
        if not self.entity_type or not str(self.entity_type).strip():
            raise ValueError("ObjectRule.entity_type is required")


@dataclass
class EntityExtractionOverlay:
    """Customer-calibrated extraction rules for one connector.

    Loaded by the entity extractor at runtime. One overlay per
    (org_id, connector_id) pair — see overlay_registry.py.

    The list/dict fields default to empty so a partial overlay (e.g. the common
    nCino starting point, or a test fixture) can be constructed without supplying
    every category. Customer overlays extend the common nCino patterns; they
    never replace the core extractor.
    """

    org_id: str
    connector_id: str               # 'salesforce' | 'servicenow' | 'jira' etc.
    version: str = "1.0.0"          # semver — tracked for auditability

    # Person entity extraction rules.
    person_fields: List[PersonFieldRule] = field(default_factory=list)

    # Team entity extraction rules.
    team_fields: List[TeamFieldRule] = field(default_factory=list)

    # Object/Process entity extraction rules.
    object_rules: List[ObjectRule] = field(default_factory=list)

    # Customer stage name → canonical stage name.
    stage_map: Dict[str, str] = field(default_factory=dict)

    # Service-account display-name patterns to filter (regex strings).
    service_account_patterns: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.org_id or not str(self.org_id).strip():
            raise ValueError("EntityExtractionOverlay.org_id is required")
        if not self.connector_id or not str(self.connector_id).strip():
            raise ValueError("EntityExtractionOverlay.connector_id is required")
        if not self.version or not str(self.version).strip():
            raise ValueError("EntityExtractionOverlay.version is required")

    def referenced_object_names(self) -> set[str]:
        """Return the set of object API names referenced by any rule.

        Used by the extractor to index source records by object type efficiently.
        """
        names: set[str] = set()
        for rule in self.person_fields:
            names.add(rule.object_api_name)
        for rule in self.team_fields:
            names.add(rule.object_api_name)
        for rule in self.object_rules:
            names.add(rule.object_api_name)
        return names
