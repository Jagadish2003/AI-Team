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
``sf_crm`` (Salesforce CRM) -> ``sn`` (ServiceNow) -> ``jira`` -> ``sf_ncino``
(the second Salesforce pass for nCino/declared product) -> ``detect`` ->
``enrich`` -> ``complete``.
"""
from __future__ import annotations

from typing import List

DISCOVERY_STEPS: List[str] = [
    "sf_crm",    # after salesforce.ingest()
    "sn",        # after servicenow.ingest()
    "jira",      # after jira_mod.ingest()
    "sf_ncino",  # after ncino_ingest() (second Salesforce pass)
    "detect",    # after _run_detector_phase()
    "enrich",    # before entity extraction / LLM enrichment
    "complete",  # at the final return
]

# Membership-check form for O(1) step_id validation in db.update_run_step().
DISCOVERY_STEP_IDS = frozenset(DISCOVERY_STEPS)
