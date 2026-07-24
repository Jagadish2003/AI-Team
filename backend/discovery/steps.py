"""Canonical discovery step identifiers (CS-4 T3).

Single source of truth for the ordered discovery-stage IDs emitted by
``discovery/runner.py`` (via ``db.update_run_step()``), surfaced by
``GET /api/runs/{run_id}/status`` as ``current_step``, and rendered by the
frontend ``DISCOVERY_STEPS`` progress list on ``DiscoveryRunPage``.

This lives in the discovery layer (not ``app/db.py``) because the step
vocabulary is owned by the discovery pipeline that emits it. It is deliberately
a dependency-free leaf module: it imports nothing from ``app`` or ``database``,
so ``app.db`` can import it for step validation without creating an import
cycle (``discovery.runner`` already imports ``app.db``; this module never
imports back into ``app``).

The list order matches the exact order ``runner.run()`` emits the steps:
``sf_crm`` (Salesforce CRM) -> ``sn`` (ServiceNow) -> ``jira`` -> ``slack`` ->
``teams`` -> ``confluence`` -> ``sharepoint`` -> ``github`` -> ``java_app`` ->
``dotnet_app`` (each connected conversation / knowledge / operational source, in
ingest order) -> ``sf_ncino`` (the pack-specific second Salesforce pass for the
declared product) -> ``detect`` -> ``enrich`` -> ``complete``.

Every connected SOURCE emits its own step at the START of its ingest, so the
Discovery Progress list shows exactly ONE source in-progress (a spinner) at a
time — the connector currently being ingested — with earlier sources completed
and later sources pending. The pack-specific second Salesforce pass
(``sf_ncino``, labelled by the selected pack — Service Cloud / nCino / etc.) is
emitted last among the ingest steps, so the list shows every connected source
before the selected pack.
"""
from __future__ import annotations

from typing import List

DISCOVERY_STEPS: List[str] = [
    "sf_crm",      # before salesforce.ingest()
    "sn",          # before servicenow.ingest()
    "jira",        # before jira_mod.ingest()
    "slack",       # before _ingest_slack_corroboration() (Slack change-based ingest)
    "teams",       # before _ingest_teams_corroboration()
    "confluence",  # before _ingest_confluence_corroboration()
    "sharepoint",  # before _ingest_sharepoint_corroboration()
    "github",      # before _ingest_github()
    "java_app",    # before _ingest_java_app_corroboration()
    "dotnet_app",  # before _ingest_dotnet_app_corroboration()
    "sf_ncino",    # after ncino_ingest() — pack-specific second Salesforce pass
    "detect",      # after _run_detector_phase()
    "enrich",      # before entity extraction / LLM enrichment
    "complete",    # at the final return
]

# Membership-check form for O(1) step_id validation in db.update_run_step().
DISCOVERY_STEP_IDS = frozenset(DISCOVERY_STEPS)
