"""Common nCino overlay patterns — ENT-1 Task T3.

The common nCino patterns below cover the ``LLC_BI__*`` field conventions that
apply across most nCino implementations. They are a strong *default foundation*,
not a complete customer overlay.

Customer-specific field names (custom Loan Officer fields, proprietary stage
names, customer service-account naming) are added in the customer's own overlay
file by the implementation engineer after the Session 1 environment profile —
see docs/entity_overlay_authoring.md. The customer overlay EXTENDS these common
patterns; it never replaces them, and the core pack is never modified for a
customer.

Person ID fields resolve with confidence 1.0 because a Salesforce ID is a
stronger anchor than name-only matching.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base_overlay import (
    EntityExtractionOverlay,
    ObjectRule,
    PersonFieldRule,
    TeamFieldRule,
)

# nCino runs on Salesforce — the common overlay targets the 'salesforce' connector.
NCINO_CONNECTOR_ID = "salesforce"

# Common nCino person fields. ID-based fields resolve with confidence 1.0.
NCINO_COMMON_PERSON_FIELDS: List[PersonFieldRule] = [
    PersonFieldRule(
        object_api_name="LLC_BI__Loan__c",
        field_api_name="OwnerId",
        resolution_source="id",  # Salesforce ID — confidence 1.0
        label="Loan Owner",
    ),
    PersonFieldRule(
        object_api_name="LLC_BI__Loan__c",
        field_api_name="LLC_BI__Loan_Officer__c",
        resolution_source="id",
        label="Loan Officer",
    ),
    PersonFieldRule(
        object_api_name="LLC_BI__Covenant__c",
        field_api_name="LLC_BI__Covenant_Analyst__c",
        resolution_source="id",
        label="Covenant Analyst",
    ),
]

# Common nCino object rules — the important business objects in a lending flow.
NCINO_COMMON_OBJECT_RULES: List[ObjectRule] = [
    ObjectRule(
        object_api_name="LLC_BI__Covenant__c",
        entity_type="object",
        name_field="Name",
        record_type="Covenant",
    ),
    ObjectRule(
        object_api_name="LLC_BI__Loan__c",
        entity_type="object",
        name_field="Name",
        record_type="Loan",
    ),
]

# Common nCino team fields. Empty by default — most team structure is
# customer-specific and added in the customer overlay after Session 1.
NCINO_COMMON_TEAM_FIELDS: List[TeamFieldRule] = []

# Service-account display-name patterns common to nCino implementations.
# These filter integration / system / API / automation users out of the entity
# graph so downstream graph and LLM logic is not misled by non-human actors.
NCINO_SERVICE_ACCOUNT_PATTERNS: List[str] = [
    r"^integration[_\s]user",
    r"^system[_\s]admin",
    r"^salesforce[_\s]admin",
    r"^nCino[_\s]",
    r"^api[_\s]user",
    r"^batch[_\s]user",
    r"^automation[_\s]",
]


def build_ncino_overlay(
    org_id: str,
    connector_id: str = NCINO_CONNECTOR_ID,
    version: str = "1.0.0",
    *,
    extra_person_fields: Optional[List[PersonFieldRule]] = None,
    extra_team_fields: Optional[List[TeamFieldRule]] = None,
    extra_object_rules: Optional[List[ObjectRule]] = None,
    stage_map: Optional[Dict[str, str]] = None,
    extra_service_account_patterns: Optional[List[str]] = None,
) -> EntityExtractionOverlay:
    """Build an nCino overlay for an org from the common patterns plus extras.

    This is the recommended starting point for a customer overlay: it seeds the
    common nCino person fields, object rules, and service-account patterns, then
    appends any customer-specific rules collected in the Session 1 environment
    profile. The common lists are copied, never mutated, so registering one
    customer overlay can never affect another.
    """
    return EntityExtractionOverlay(
        org_id=org_id,
        connector_id=connector_id,
        version=version,
        person_fields=[*NCINO_COMMON_PERSON_FIELDS, *(extra_person_fields or [])],
        team_fields=[*NCINO_COMMON_TEAM_FIELDS, *(extra_team_fields or [])],
        object_rules=[*NCINO_COMMON_OBJECT_RULES, *(extra_object_rules or [])],
        stage_map=dict(stage_map or {}),
        service_account_patterns=[
            *NCINO_SERVICE_ACCOUNT_PATTERNS,
            *(extra_service_account_patterns or []),
        ],
    )
