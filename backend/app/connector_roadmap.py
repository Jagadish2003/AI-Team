"""Integration Hub connector roadmap calibration — R191-R1 T5 (AT-726).

Single source of truth for the **anchor-on-shipped** rule applied to the
Integration Hub connector catalog:

    Integration Hub catalog tiles anchor only on connectors that ship today.
    Everything else is explicitly labelled roadmap — never silently advertised
    as connectable.

A catalog tile whose ingestion does NOT ship yet (SAP and Dynamics 365 have an
auth/config but no ingestor; a handful of aspirational tiles have neither) is
**roadmap-labelled and non-connectable** — the Hub shows "Coming — <target>"
and the connect action is disabled/refused. SAP and Dynamics 365 are demand-gated
for **2.0.1** (CEO decision, Release 1.9.1 §8); every other unshipped tile is
`unscheduled` until a release commits to it.

This mirrors the Stack Builder registry's `RoadmapSystemConfig`
(`backend/discovery/packs/industry_registry.py`, target_release="2.0.1" for
SAP/D365, "unscheduled" for GitLab) so the two surfaces stay honest together.

Design notes
------------
* This module is the ONE place that declares which catalog connectors ship. It
  is applied as a serve-time overlay in `db.org_connectors_list` /
  `org_connector_get`, so the roadmap flags reach every catalog consumer
  (`GET /api/connectors`, the workspace-catalog, the connect guard) without a
  reseed and without hardcoding the state into the `connectors` seed.
* `SHIPPED_CONNECTOR_IDS` is an explicit, justified allow-list — the same style
  as `ENABLED_CONNECTOR_IDS` on the frontend tile. The *dynamically discovered*
  CI cross-check against `backend/discovery/ingest/` (R191-R1 AC1, "the real
  deliverable") is a separate, later task; this interim set keeps the catalog
  honest in the meantime. When 2.0.1 ships an ingestor, adding its id here (and
  dropping any `ROADMAP_TARGETS` entry) flips the tile to shipped — config only.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# Catalog connector ids (see backend/database/seed/connectors.json) whose
# ingestion ships TODAY. A tile NOT in this set is roadmap.
#
#   salesforce/servicenow/jira        — discovery/ingest/{salesforce,servicenow,jira}.py
#   github                            — discovery/ingest/git_content.py + connectors/saas/github.py
#   slack/teams/confluence/sharepoint — discovery/ingest/{slack,teams,confluence,sharepoint}.py
#   postgresql/sql_server/oracle_db   — native DB connectors under connectors/db/
#   aws_events                        — discovery/ingest/aws_event_connector.py (MSP-B1)
#   azure_events                      — discovery/ingest/azure_events.py (MSP-B2)
#
# NOTE: a shipped connector may still be product-gated OFF the Hub connect button
# (see ENABLED_CONNECTOR_IDS on the frontend tile — e.g. the native databases).
# "Shipped" here means "ingestion exists", which is what separates a product gate
# ("currently unavailable") from a roadmap tile ("Coming — <target>").
SHIPPED_CONNECTOR_IDS = frozenset(
    {
        "salesforce",
        "servicenow",
        "jira",
        "github",
        "slack",
        "teams",
        "confluence",
        "sharepoint",
        "postgresql",
        "sql_server",
        "oracle_db",
        "aws_events",
        "azure_events",
    }
)

# The roadmap catalog tiles and their release target. SAP and Dynamics 365 are
# demand-gated for 2.0.1 (Release 1.9.1 §8 / R191-R1); every other catalog tile
# without a shipped ingestor is unscheduled until a release commits to it.
#
# This is enumerated EXPLICITLY (like ENABLED_CONNECTOR_IDS on the frontend tile)
# rather than derived as "any id not in SHIPPED": an id that is neither shipped
# nor a known catalog tile — a test fixture, or a connector not yet catalogued —
# must NOT be mistaken for roadmap and wrongly blocked from connecting. Every id
# here is a real `connectors.json` catalog tile whose ingestion does not ship
# (verified by `test_catalog_is_fully_classified`, which cross-checks the seed).
# The dynamically-discovered ingestor cross-check is R191-R1 AC1 (a later task).
# Canonical release-target constant for the SAP/D365 demand-gated tiles. Public
# (no leading underscore) and imported by the Stack Builder registry
# (discovery/packs/industry_registry.py) so the target string lives in ONE place —
# change the release here and both the catalog and the registry move together.
TARGET_2_0_1 = "2.0.1"
UNSCHEDULED_TARGET = "unscheduled"

ROADMAP_TARGETS: Dict[str, str] = {
    # Demand-gated for 2.0.1 — auth/config exists, ingestion does not (AT-726).
    "sap": TARGET_2_0_1,
    "dynamics365": TARGET_2_0_1,
    # Other catalog tiles without a shipped ingestor — roadmap, unscheduled.
    "databricks": UNSCHEDULED_TARGET,
    "oracle_ebs": UNSCHEDULED_TARGET,
    "workday": UNSCHEDULED_TARGET,
    "azure_devops": UNSCHEDULED_TARGET,
    "linear": UNSCHEDULED_TARGET,
    "zendesk": UNSCHEDULED_TARGET,
    "m365": UNSCHEDULED_TARGET,
    "notion": UNSCHEDULED_TARGET,
    "gitlab": UNSCHEDULED_TARGET,
    "bitbucket": UNSCHEDULED_TARGET,
    "azure_repos": UNSCHEDULED_TARGET,
    "snowflake": UNSCHEDULED_TARGET,
    "dbt": UNSCHEDULED_TARGET,
}

# The roadmap tile ids (membership is authoritative; unknown ids are NOT roadmap).
ROADMAP_CONNECTOR_IDS = frozenset(ROADMAP_TARGETS)


def is_shipped(connector_id: str) -> bool:
    """True when the connector's ingestion ships today (not a roadmap tile)."""
    return connector_id in SHIPPED_CONNECTOR_IDS


def is_roadmap(connector_id: str) -> bool:
    """True when the connector is a known roadmap catalog tile — the Hub labels
    it "Coming — <target>" and refuses to connect it. An unknown id (test fixture
    / not-yet-catalogued connector) is NOT roadmap, so it is never wrongly blocked."""
    return connector_id in ROADMAP_CONNECTOR_IDS


def roadmap_target(connector_id: str) -> str:
    """Roadmap release target for a connector. SAP/D365 → '2.0.1'; every other
    unshipped connector → 'unscheduled'. (Meaningful only when `is_roadmap`.)"""
    return ROADMAP_TARGETS.get(connector_id, UNSCHEDULED_TARGET)


def annotate_connector(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a catalog connector row stamped with roadmap flags.

    Adds two camelCase fields (matching the catalog's existing contract style —
    `recommendedRank`, `signalStrength`, `lastSynced`):

        roadmap        : bool           — True ⇒ non-connectable roadmap tile
        roadmapTarget  : str | None     — release target when roadmap, else None

    Never mutates `status` or `tier`: the roadmap state is an ADDITIVE signal the
    frontend renders from, so existing per-org connection state and the
    workspace-catalog grouping are untouched.
    """
    connector_id = str(row.get("id") or row.get("system_id") or "")
    # is_roadmap("") is already False (the empty string is not a roadmap id), so no
    # separate truthiness guard is needed.
    roadmap = is_roadmap(connector_id)
    return {
        **row,
        "roadmap": roadmap,
        "roadmapTarget": roadmap_target(connector_id) if roadmap else None,
    }


def roadmap_block_message(connector_id: str, name: Optional[str] = None) -> str:
    """User-facing reason a roadmap connector cannot be connected — names the
    connector and its target release so support sees what happened."""
    label = name or connector_id
    target = roadmap_target(connector_id)
    if target == UNSCHEDULED_TARGET:
        return (
            f"{label} is on the AgentIQ roadmap and is not yet connectable."
        )
    return (
        f"{label} is on the AgentIQ roadmap (coming in {target}) and is not yet "
        f"connectable."
    )
