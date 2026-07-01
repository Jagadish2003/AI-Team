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
``sf_crm`` (Salesforce CRM) -> ``sn`` (ServiceNow) -> ``jira`` -> ``slack``
(Slack change-based ingest) -> ``sf_ncino`` (the pack-specific second Salesforce
pass for the declared product) -> ``detect`` -> ``enrich`` -> ``complete``.

All connected SOURCES (the systems of record plus conversation sources like
Slack, and Teams when it lands) are emitted first; the pack-specific second
Salesforce pass (``sf_ncino``, labelled by the selected pack — Service Cloud /
nCino / etc.) is emitted last among the ingest steps, so the Discovery Progress
list shows every connected source before the selected pack.
"""
from __future__ import annotations

from typing import List

DISCOVERY_STEPS: List[str] = [
    "sf_crm",    # after salesforce.ingest()
    "sn",        # after servicenow.ingest()
    "jira",      # after jira_mod.ingest()
    "slack",     # after _ingest_slack_corroboration() (Slack change-based ingest)
    "sf_ncino",  # after ncino_ingest() — pack-specific second Salesforce pass
    "detect",    # after _run_detector_phase()
    "enrich",    # before entity extraction / LLM enrichment
    "complete",  # at the final return
]

# Membership-check form for O(1) step_id validation in db.update_run_step().
DISCOVERY_STEP_IDS = frozenset(DISCOVERY_STEPS)
