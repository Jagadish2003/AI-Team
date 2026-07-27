"""Canonical source-key registry (backend side).

Maps a connector / ``signal_source`` id (the stable lowercase id an ingestor or
detector writes — ``"servicenow"``, ``"azure_events"``, …) to the display
``sourceSystem`` string that appears on contract-shaped rows the frontend joins
on (``MappingRow.sourceSystem``, ``PermissionRequirement.sourceSystem``).

This module is the backend half of the pair the frontend registry
``frontend/src/utils/sourceKeys.ts`` already anticipated:

    "Sprint 5 task: create backend/app/source_keys.py with matching constants
     so ingestors import from there — single source of truth across frontend
     and backend."

The two maps MUST stay in step: the frontend derives a connector's source key
with ``sourceKeyForConnector(connector.id)`` and filters rows by equality, so a
row this module labels differently is silently invisible in the UI.
``backend/tests/contract/test_source_keys_parity.py`` parses the TypeScript
registry and fails the build if they drift.

Adding a new connector is ONE entry here plus the matching entry in the
TypeScript registry — no consumer changes. An unknown id falls through to the id
itself, exactly as ``sourceKeyForConnector`` does, so an unmapped connector still
joins consistently on both sides instead of vanishing.
"""
from __future__ import annotations

from typing import Dict

# Keep in sync with SOURCE_KEY_MAP in frontend/src/utils/sourceKeys.ts.
SOURCE_KEY_MAP: Dict[str, str] = {
    "salesforce": "Salesforce",
    "servicenow": "ServiceNow",
    "jira": "Jira",
    "confluence": "Confluence",
    "slack": "Slack",
    "databricks": "Databricks",
    "microsoft_365": "Microsoft 365",
    "sharepoint": "SharePoint",
    "github": "GitHub",
    "azure_devops": "Azure DevOps",
    "gitlab": "GitLab",
    "datadog": "Datadog",
    "splunk": "Splunk",
    "d365": "Dynamics 365",
    "ncino": "nCino",
    "sap": "SAP",
    "azure_events": "Azure Events",
    "aws_events": "AWS Events",
}

# Mirrors CONNECTOR_ID_ALIASES in the TypeScript registry.
CONNECTOR_ID_ALIASES: Dict[str, str] = {
    "jira_confluence": "jira",
}


def source_key_for(connector_id: str) -> str:
    """Return the canonical display ``sourceSystem`` for a connector/signal id.

    Falls back to the id itself for an unregistered connector — the same
    fallback ``sourceKeyForConnector`` applies — so both sides still agree.
    """
    if not connector_id:
        return ""
    canonical = CONNECTOR_ID_ALIASES.get(connector_id, connector_id)
    return SOURCE_KEY_MAP.get(canonical, connector_id)


__all__ = ["SOURCE_KEY_MAP", "CONNECTOR_ID_ALIASES", "source_key_for"]
