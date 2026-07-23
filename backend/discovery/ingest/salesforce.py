"""
SF-2.2 — Salesforce Ingestion Module

Offline mode: reads backend/discovery/ingest/fixtures/salesforce_sample.json
Live mode:    calls Salesforce REST + SOQL + Tooling APIs

Live-mode credentials come from the connector's credential record ONLY (the
per-run credential context, or the per-org vault) — the instance URL and OAuth
access token are BOTH part of that record, captured at OAuth connect. There is
no SF_INSTANCE_URL / SF_ACCESS_TOKEN environment fallback (R191-H1 / T2 — F2 fix):
connection config is part of the connector record (one source of connector truth).

SME-authored queries are documented inline per function.
All seven functions return data in the same shape regardless of mode.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import get_ingest_org, is_live
from .operational_config import CredentialRecordError

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "salesforce_sample.json"
API_VERSION = "v59.0"


# ─────────────────────────────────────────────────────────────────────────────
# Custom exception
# ─────────────────────────────────────────────────────────────────────────────


class IngestError(Exception):
    """Raised when live ingestion fails with a clear, actionable message."""


# ─────────────────────────────────────────────────────────────────────────────
# Offline fixture loader
# ─────────────────────────────────────────────────────────────────────────────


def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE_PATH.exists():
        raise IngestError(f"Fixture file not found: {FIXTURE_PATH}")
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Live HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_client() -> Optional["SalesforceClient"]:
    """Build a REST client from the OAuth-provided Salesforce credentials.

    The Salesforce instance URL and access token are sourced from the credential
    record ONLY (R191-H1 / T2 — F2 fix). Salesforce returns ``instance_url`` in
    its token response; it is captured at OAuth connect and published to the
    per-run credential context alongside the vault access token, isolated per
    org/run. With no per-run context (CLI/standalone) the credential resolves
    per-org from the vault via the single credential path
    (``get_connector_credentials``). There is **no ``SF_INSTANCE_URL`` env
    fallback** — connection config is part of the connector's credential record
    (one source of connector truth); a record without a URL is a loud
    configuration error naming the record, never a silent env default.

    The old server-key token-generation fallback (token_generation/salesforce)
    was removed — live ingest relies solely on the OAuth token. A missing or
    invalid token surfaces as a clear IngestError (the run degrades that system)
    instead of silently re-minting a token via the JWT path. An expired token is
    detected naturally when the first SOQL call returns HTTP 401.
    """
    from . import get_live_connector, resolve_vault_connector

    cred = get_live_connector("salesforce") or resolve_vault_connector("salesforce")
    org_id = get_ingest_org()

    # No credential at all: in live mode this is a clear, actionable error; offline
    # simply has no client (the fixture path is used instead).
    if not cred:
        if is_live():
            raise CredentialRecordError(
                org_id=org_id,
                connector_id="salesforce",
                missing_field="credential",
                message=(
                    "Live mode requires a Salesforce credential from the credential "
                    "vault (instance URL + OAuth access token). Connect Salesforce in "
                    "the Integration Hub, or set INGEST_MODE=offline to run without "
                    "credentials."
                ),
            )
        return None

    instance_url = cred.get("url")
    access_token = cred.get("token")

    # The instance URL is part of the connector's credential record. A record
    # without one is a configuration error surfaced loudly and named — never a
    # silent env default (R191-H1 / T2, AC4).
    if not instance_url:
        raise CredentialRecordError(
            org_id=org_id,
            connector_id="salesforce",
            missing_field="url",
            message=(
                "Salesforce credential record is missing its instance URL "
                "('salesforce' connector). The instance URL is captured at OAuth "
                "connect and stored on the credential record; reconnect Salesforce in "
                "the Integration Hub so the record carries its URL. "
                "(No SF_INSTANCE_URL environment fallback is used.)"
            ),
        )
    if not access_token:
        if is_live():
            raise CredentialRecordError(
                org_id=org_id,
                connector_id="salesforce",
                missing_field="token",
                message=(
                    "Salesforce credential record is missing its OAuth access token "
                    "('salesforce' connector). Connect Salesforce in the Integration "
                    "Hub, or set INGEST_MODE=offline to run without credentials."
                ),
            )
        return None

    return SalesforceClient(instance_url, access_token.strip())


class SalesforceClient:
    """Thin wrapper around Salesforce REST APIs."""

    def __init__(self, instance_url: str, access_token: str):
        self.instance_url = instance_url
        self.access_token = access_token.strip() if access_token else ""
        self._session = None

    def _session_get(self):
        try:
            import requests

            if self._session is None:
                self._session = requests.Session()
                self._session.headers.update(
                    {
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    }
                )
            return self._session

        except ImportError:
            raise IngestError(
                "requests library required for live mode: pip install requests"
            )

    def soql(self, query: str) -> List[Dict]:
        """Execute a SOQL query. Returns records list."""
        import urllib.parse

        session = self._session_get()
        url = f"{self.instance_url}/services/data/{API_VERSION}/query/"
        params = {"q": query}
        try:
            resp = session.get(url, params=params, timeout=30)
            if not resp.ok:
                raise IngestError(
                    f"HTTP {resp.status_code}: {resp.text}\nQuery: {query}"
                )
            data = resp.json()
            records = data.get("records", [])
            # Handle pagination
            next_url = data.get("nextRecordsUrl")
            while next_url:
                resp2 = session.get(f"{self.instance_url}{next_url}", timeout=30)
                if not resp2.ok:
                    raise IngestError(f"HTTP {resp2.status_code}: {resp2.text}")
                page = resp2.json()
                records.extend(page.get("records", []))
                next_url = page.get("nextRecordsUrl")
            return records
        except IngestError:
            raise
        except Exception as e:
            raise IngestError(f"SOQL query failed: {e}\nQuery: {query}")

    def tooling_soql(self, query: str, max_records: int = 5000) -> List[Dict]:
        """
        Execute a Tooling API SOQL query with pagination.

        max_records: safety cap to prevent runaway fetches on large orgs
        (default 5000 covers all realistic Flow/NC inventories).
        Raise IngestError if the result set exceeds max_records.
        """
        session = self._session_get()
        url = f"{self.instance_url}/services/data/{API_VERSION}/tooling/query/"
        try:
            resp = session.get(url, params={"q": query}, timeout=60)
            if not resp.ok:
                raise IngestError(
                    f"HTTP {resp.status_code}: {resp.text}\nQuery: {query}"
                )
            data = resp.json()
            records = data.get("records", [])
            next_url = data.get("nextRecordsUrl")
            while next_url:
                if len(records) >= max_records:
                    raise IngestError(
                        f"Tooling API result exceeded {max_records} records. "
                        f"Add a WHERE clause to narrow the query."
                    )
                resp2 = session.get(f"{self.instance_url}{next_url}", timeout=60)
                if not resp2.ok:
                    raise IngestError(f"HTTP {resp2.status_code}: {resp2.text}")
                page = resp2.json()
                records.extend(page.get("records", []))
                next_url = page.get("nextRecordsUrl")
            return records
        except IngestError:
            raise
        except Exception as e:
            raise IngestError(f"Tooling API query failed: {e}\nQuery: {query}")


# ─────────────────────────────────────────────────────────────────────────────
# Seven ingestion functions
# ─────────────────────────────────────────────────────────────────────────────


def get_case_metrics(client: Optional[SalesforceClient] = None) -> Dict[str, Any]:
    """
    Pull Case volume, owner handoff counts, and knowledge coverage.

    SME SOQL queries:
        -- Total cases in 90-day window
        SELECT COUNT(Id) total FROM Case WHERE CreatedDate = LAST_N_DAYS:90

        -- Owner changes (handoffs) from CaseHistory
        SELECT COUNT(Id) changes FROM CaseHistory
        WHERE Field = 'Owner' AND CreatedDate = LAST_N_DAYS:90

        -- Cases with linked Knowledge Articles
        SELECT COUNT_DISTINCT(CaseId) linked FROM CaseArticle
        WHERE CreatedDate = LAST_N_DAYS:90

        -- Closed cases in window
        SELECT COUNT(Id) closed FROM Case
        WHERE Status = 'Closed' AND CreatedDate = LAST_N_DAYS:90

        -- Category breakdown
        SELECT Reason, COUNT(Id) volume FROM Case
        WHERE CreatedDate = LAST_N_DAYS:90 GROUP BY Reason

    Returns: case_metrics dict matching salesforce_sample.json shape
    """
    if not is_live():
        return _load_fixture()["case_metrics"]

    total_recs = client.soql(
        "SELECT COUNT(Id) FROM Case WHERE CreatedDate = LAST_N_DAYS:90"
    )
    total_cases = total_recs[0].get("expr0", 0) if total_recs else 0

    change_recs = client.soql(
        "SELECT COUNT(Id) FROM CaseHistory "
        "WHERE Field = 'Owner' AND CreatedDate = LAST_N_DAYS:90"
    )
    owner_changes = change_recs[0].get("expr0", 0) if change_recs else 0

    closed_recs = client.soql(
        "SELECT COUNT(Id) FROM Case WHERE Status = 'Closed' "
        "AND CreatedDate = LAST_N_DAYS:90"
    )
    closed_cases = closed_recs[0].get("expr0", 0) if closed_recs else 0

    kb_recs = client.soql(
        "SELECT COUNT_DISTINCT(CaseId) FROM CaseArticle "
        "WHERE CreatedDate = LAST_N_DAYS:90"
    )
    cases_with_kb = kb_recs[0].get("expr0", 0) if kb_recs else 0

    handoff_score = round(owner_changes / total_cases, 4) if total_cases > 0 else 0.0
    knowledge_gap_score = (
        round(1 - (cases_with_kb / closed_cases), 4) if closed_cases > 0 else 0.0
    )

    # Category breakdown
    cat_recs = client.soql(
        "SELECT Reason, COUNT(Id) FROM Case "
        "WHERE CreatedDate = LAST_N_DAYS:90 GROUP BY Reason"
    )
    category_breakdown = [
        {
            "category": r.get("Reason", "Unknown"),
            "volume": r.get("expr0", 0),
            "handoff_score": 0.0,
            "avg_age_days": 0.0,
        }
        for r in cat_recs
    ]

    return {
        "total_cases_90d": total_cases,
        "closed_cases_90d": closed_cases,
        "owner_changes_90d": owner_changes,
        "handoff_score": handoff_score,
        "cases_with_kb_link": cases_with_kb,
        "knowledge_gap_score": knowledge_gap_score,
        "category_breakdown": category_breakdown,
    }


def get_flow_inventory(client: Optional[SalesforceClient] = None) -> Dict[str, Any]:
    """
    Pull active AutoLaunchedFlows on high-volume objects.

    SME SOQL queries:
        -- Active flows
        SELECT ActiveVersionId, Label, ProcessType, TriggerType,
               TriggerObjectOrEventLabel FROM FlowDefinitionView
        WHERE IsActive = true

        -- Element count from Flow Metadata (heavy — paginate via Tooling API)
        SELECT Id, MasterLabel, Metadata FROM Flow WHERE Status = 'Active'

    Returns: flow_inventory dict matching salesforce_sample.json shape
    """
    if not is_live():
        return _load_fixture()["flow_inventory"]

    # FlowDefinitionView holds the Trigger properties, not FlowVersionView
    flow_recs = client.soql(
        "SELECT ActiveVersionId, Label, ProcessType, TriggerType, "
        "TriggerObjectOrEventLabel FROM FlowDefinitionView "
        "WHERE IsActive = true"
    )

    auto_launched = [
        r
        for r in flow_recs
        if r.get("ProcessType") == "AutoLaunchedFlow"
        and r.get("TriggerObjectOrEventLabel") == "Case"
        and (
            r.get("TriggerType") == "RecordAfterSave"
            or r.get("TriggerType") == "RecordBeforeSave"
            or r.get("TriggerType") == "null"
        )
    ]

    # Element counts from Metadata (best-effort; may be slow on large orgs)
    element_counts = []
    for r in auto_launched:
        try:
            flow_version_id = r.get("ActiveVersionId")
            if not flow_version_id:
                element_counts.append(0)
                continue

            meta_recs = client.tooling_soql(
                f"SELECT Id, MasterLabel, Metadata FROM Flow "
                f"WHERE Id = '{flow_version_id}'"
            )
            if meta_recs and meta_recs[0].get("Metadata"):
                meta = meta_recs[0]["Metadata"]
                # Count all element arrays in flow metadata
                count = sum(
                    len(meta.get(k, []))
                    for k in [
                        "decisions",
                        "loops",
                        "recordCreates",
                        "recordDeletes",
                        "recordLookups",
                        "recordUpdates",
                        "assignments",
                        "subflows",
                        "actionCalls",
                    ]
                )
                element_counts.append(count)
            else:
                element_counts.append(0)
        except Exception:
            element_counts.append(0)

    avg_elements = (
        round(sum(element_counts) / len(element_counts), 2) if element_counts else 0.0
    )

    try:
        case_recs = client.soql(
            "SELECT COUNT(Id) FROM Case WHERE CreatedDate = LAST_N_DAYS:90"
        )
        records_90d = case_recs[0].get("expr0", 0) if case_recs else 0
    except Exception:
        records_90d = 0

    flow_activity_score = (
        round((records_90d / 90) * (len(auto_launched) / max(avg_elements, 1)), 4)
        if auto_launched
        else 0.0
    )

    return {
        "active_flow_count_on_object": len(auto_launched),
        "avg_element_count": avg_elements,
        "flow_activity_score": flow_activity_score,
        "trigger_object": "Case",
        "records_90d": records_90d,
        "flows": [
            {
                "flow_id": r.get("ActiveVersionId") or "",
                "flow_label": r.get("Label") or "",
                "process_type": r.get("ProcessType") or "",
                "element_count": element_counts[i] if i < len(element_counts) else 0,
                "trigger_object": r.get("TriggerObjectOrEventLabel", ""),
            }
            for i, r in enumerate(auto_launched)
        ],
    }


def get_approval_pending(
    client: Optional[SalesforceClient] = None,
) -> List[Dict[str, Any]]:
    """
    Pull pending ProcessInstance records with step age and approver count.

    SME SOQL queries:
        -- Pending approvals
        SELECT ProcessDefinition.Name, Status, CreatedDate
        FROM ProcessInstance WHERE Status = 'Pending' LIMIT 1000

        -- Approvers per pending instance
        SELECT ProcessInstanceId, ActorId, Actor.Type
        FROM ProcessInstanceWorkitem

    Returns: list of approval process dicts matching salesforce_sample.json shape
    """
    if not is_live():
        return _load_fixture()["approval_processes"]

    # soql() handles pagination — no LIMIT needed
    pi_recs = client.soql(
        "SELECT Id, ProcessDefinition.Name, Status, CreatedDate "
        "FROM ProcessInstance WHERE Status = 'Pending'"
    )

    # Group by process name
    by_process: Dict[str, Dict] = {}
    now = datetime.now(timezone.utc)
    for r in pi_recs:
        name = (r.get("ProcessDefinition") or {}).get("Name", "Unknown")
        created = r.get("CreatedDate", "")
        pi_id = r.get("Id", "")
        age_days = 0.0
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_days = (now - dt).days
            except Exception:
                pass
        if name not in by_process:
            by_process[name] = {
                "process_name": name,
                "pending_count": 0,
                "total_age_days": 0.0,
                "pi_ids": [],
            }
        by_process[name]["pending_count"] += 1
        by_process[name]["total_age_days"] += age_days
        by_process[name]["pi_ids"].append(pi_id)

    # ProcessInstanceWorkitem: ActorId can be User, Role, Queue, or Group.
    # approver_count = distinct ActorId values — may undercount human capacity
    # when Roles/Queues are used. approver_type_notes flags this explicitly.
    wi_recs = client.soql(
        "SELECT ProcessInstanceId, ActorId, Actor.Type FROM ProcessInstanceWorkitem"
    )

    # Map: process_name -> {actor_id -> actor_type}
    pi_to_process = {
        pi_id: name for name, info in by_process.items() for pi_id in info["pi_ids"]
    }
    actor_map: Dict[str, Dict[str, str]] = {}  # process_name -> {actor_id: actor_type}
    for w in wi_recs:
        pid = w.get("ProcessInstanceId", "")
        actor_id = w.get("ActorId", "")
        actor_type = (w.get("Actor") or {}).get("Type", "User")
        proc_name = pi_to_process.get(pid)
        if proc_name and actor_id:
            actor_map.setdefault(proc_name, {})[actor_id] = actor_type

    results = []
    for name, info in by_process.items():
        cnt = info["pending_count"]
        avg_delay = round(info["total_age_days"] / cnt, 2) if cnt > 0 else 0.0

        actors = actor_map.get(name, {})
        approver_count = len(actors)

        # Summarise actor types for scorer confidence flag
        type_counts: Dict[str, int] = {}
        for t in actors.values():
            type_counts[t] = type_counts.get(t, 0) + 1

        has_non_user = any(t != "User" for t in actors.values())
        approver_type_notes = (
            "Contains Role/Queue/Group actors — approver_count undercounts human capacity. "
            "D3/D6 confidence should be capped at MEDIUM."
            if has_non_user
            else "All actors are User type — approver_count is reliable."
        )

        bottleneck_score = (
            round(cnt / approver_count, 2) if approver_count > 0 else float(cnt)
        )

        results.append(
            {
                "process_name": name,
                "pending_count": cnt,
                "avg_delay_days": avg_delay,
                "approver_count": approver_count,
                "bottleneck_score": bottleneck_score,
                "approver_ids": list(actors.keys()),
                "approver_type_breakdown": type_counts,
                "approver_type_notes": approver_type_notes,
            }
        )

    return results


def get_knowledge_coverage(client: Optional[SalesforceClient] = None) -> Dict[str, Any]:
    """
    Pull knowledge gap metrics — already included in get_case_metrics().
    Returns the relevant sub-section for direct detector access.

    Returns: dict with closed_cases_90d, cases_with_kb_link, knowledge_gap_score
    """
    if not is_live():
        cm = _load_fixture()["case_metrics"]
        return {
            "closed_cases_90d": cm["closed_cases_90d"],
            "cases_with_kb_link": cm["cases_with_kb_link"],
            "knowledge_gap_score": cm["knowledge_gap_score"],
        }

    cm = get_case_metrics(client)
    return {
        "closed_cases_90d": cm["closed_cases_90d"],
        "cases_with_kb_link": cm["cases_with_kb_link"],
        "knowledge_gap_score": cm["knowledge_gap_score"],
    }


def get_named_credentials(
    client: Optional[SalesforceClient] = None,
) -> List[Dict[str, Any]]:
    """
    Pull the Named Credential catalog from the org via Tooling API.

    SME Tooling API query:
        SELECT Id, DeveloperName, MasterLabel, Endpoint, PrincipalType
        FROM NamedCredential

    Returns: list of dicts with credential_name, credential_developer_name,
    endpoint, principal_type. Does NOT include flow references — call
    get_named_credential_flow_refs() and merge the results for D5 detection.
    """
    if not is_live():
        # Offline: return catalog portion only (flow_reference_count added by flow_refs fn)
        return _load_fixture()["named_credentials"]

    nc_recs = client.tooling_soql(
        "SELECT Id, DeveloperName, MasterLabel, Endpoint, PrincipalType "
        "FROM NamedCredential"
    )
    return [
        {
            "credential_name": r.get("MasterLabel", ""),
            "credential_developer_name": r.get("DeveloperName", ""),
            "endpoint": r.get("Endpoint", ""),
            "principal_type": r.get("PrincipalType", ""),
        }
        for r in nc_recs
    ]


# ── Named credential field inspection helpers ─────────────────────────────────

# Exact Metadata sub-fields where Salesforce stores Named Credential references.
# Each entry is (parent_array_key, child_dict_key_that_holds_credential_devname).
# These are the ONLY fields inspected — no broad string scan of full Metadata.
_NC_FIELD_PATHS: List[tuple] = [
    # HTTP Callout Actions in flows (most common — Flow Builder external service)
    ("actionCalls", "connector"),  # actionCalls[*].connector = devName
    (
        "actionCalls",
        "namedCredential",
    ),  # alternative field name used in some API versions
    # Apex actions that receive Named Credential as input variable
    ("apexPluginCalls", "namedCredential"),
    # ExternalService-backed action calls
    ("externalServiceActions", "namedCredential"),
]

# False-positive guard: these strings appear in many flows and are NOT credentials.
_NC_FALSE_POSITIVE_TOKENS = {
    "null",
    "true",
    "false",
    "Id",
    "Name",
    "Status",
    "OwnerId",
    "CreatedDate",
    "LastModifiedDate",
    "IsActive",
}


def _flow_references_credential(
    metadata: Dict[str, Any],
    dev_name: str,
    label: str,
) -> str:
    """
    Return match_type string if the flow Metadata references this credential,
    else return empty string.

    Strategy (in priority order):
      1. FIELD_EXACT — dev_name found in a known Metadata field path (highest confidence)
      2. LABEL_FIELD — label found in a known field path (medium confidence)
      3. No match — return ""

    Deliberately NOT doing:
      - Full JSON string scan (too many false positives from DeveloperName
        appearing in unrelated string literals)
      - Endpoint URL matching (orgs reuse endpoints across credentials)
      - Dynamic/computed references (Apex variables, formula fields) — not detectable
    """
    if not dev_name or dev_name in _NC_FALSE_POSITIVE_TOKENS:
        return ""

    for array_key, field_key in _NC_FIELD_PATHS:
        items = metadata.get(array_key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            val = item.get(field_key, "")
            if isinstance(val, str):
                if dev_name and val == dev_name:
                    return "field_exact"
                if label and val == label:
                    return "label_field"

    return ""


# def get_named_credential_flow_refs(
#     named_credentials: List[Dict[str, Any]],
#     client: Optional[SalesforceClient] = None,
# ) -> List[Dict[str, Any]]:
#     """
#     Scan active Flow.Metadata for references to each Named Credential (D5 signal).
#
#     DETECTION STRATEGY — v1 field-level inspection (NOT full-JSON string scan):
#     Rather than serialising the entire Metadata blob and string-searching it
#     (which produces false positives from DeveloperNames in unrelated fields),
#     this function inspects only the known Salesforce Metadata sub-fields where
#     Named Credential references actually live:
#
#         actionCalls[*].connector
#         actionCalls[*].namedCredential
#         apexPluginCalls[*].namedCredential
#         externalServiceActions[*].namedCredential
#
#     Match types returned (stored in match_type field):
#         "field_exact"  — dev_name matched in a known field (highest confidence)
#         "label_field"  — MasterLabel matched in a known field (medium confidence)
#         "none"         — no match found
#
#     KNOWN LIMITATIONS (documented explicitly so SF-3.2 can extend):
#         - Apex actions that build credential names dynamically at runtime
#           cannot be detected statically.
#         - Platform Event triggered flows that reference credentials indirectly
#           will be missed.
#         - Named Credentials referenced only in Screen Flow HTTP actions
#           (not AutoLaunchedFlow) are included — they inflate D5 if present.
#         - Managed package flows with null Metadata are skipped silently and
#           logged at DEBUG level.
#         - The _NC_FIELD_PATHS list is fixed for Salesforce API v59.0 — later
#           API versions may add new field paths. Review at each API version bump.
#
#     In offline mode: returns named_credentials list unchanged (fixture already
#     contains flow_reference_count and referencing_flow_ids).
#
#     SME Tooling API query:
#         SELECT Id, MasterLabel, Metadata FROM Flow WHERE Status = 'Active'
#         (paginated — large orgs may have 500+ active flows)
#     """
#     if not is_live():
#         return named_credentials
#
#     flow_meta: List[Dict] =[]
#     try:
#         active_flows = client.tooling_soql("SELECT Id, MasterLabel FROM Flow WHERE Status='Active'")
#         for f in active_flows:
#             try:
#                 meta_recs = client.tooling_soql(f"SELECT Id, MasterLabel, Metadata FROM Flow WHERE Id='{f['Id']}'")
#                 if meta_recs:
#                     flow_meta.extend(meta_recs)
#             except Exception as e:
#                 logger.debug(f"Skipping Flow {f['Id']} metadata: {e}")
#     except Exception as e:
#         raise IngestError(f"Flow metadata fetch failed: {e}")
#
#     logger.info(f"Flow metadata: {len(flow_meta)} active flows fetched for credential scan")
#
#     results =[]
#     for nc in named_credentials:
#         dev_name = nc.get("credential_developer_name", "")
#         label = nc.get("credential_name", "")
#         referencing_ids: List[str] =[]
#         match_types: List[str] =[]
#
#         for fm in flow_meta:
#             metadata = fm.get("Metadata")
#             if not metadata:
#                 logger.debug(f"Flow {fm.get('Id')} has null Metadata — skipped in NC scan")
#                 continue
#             mtype = _flow_references_credential(metadata, dev_name, label)
#             if mtype:
#                 referencing_ids.append(fm["Id"])
#                 match_types.append(mtype)
#
#         # Dominant match type: field_exact > label_field > none
#         dominant = "field_exact" if "field_exact" in match_types else \
#                    "label_field" if "label_field" in match_types else "none"
#
#         results.append({
#             **nc,
#             "flow_reference_count": len(referencing_ids),
#             "referencing_flow_ids": referencing_ids,
#             "match_type": dominant,
#         })
#
#     return results


# ── Named credential field inspection helpers ─────────────────────────────────

# Note: Old _NC_FIELD_PATHS, _NC_FALSE_POSITIVE_TOKENS, and _flow_references_credential
# functions have been removed. We now use the MetadataComponentDependency API.


def _build_flow_ref_results(
    named_credentials: List[Dict[str, Any]],
    nc_to_flow_ids: Dict[str, Any],
    match_type: str,
) -> List[Dict[str, Any]]:
    """Attach flow_reference_count / referencing_flow_ids to each credential.

    Shared by both the dependency-API path and the per-flow-scan fallback so the
    output shape (and the D5 INTEGRATION_CONCENTRATION contract) is identical
    regardless of how the references were resolved.
    """
    results: List[Dict[str, Any]] = []
    for nc in named_credentials:
        dev_name = nc.get("credential_developer_name", "")
        referencing_ids = list(nc_to_flow_ids.get(dev_name, []))
        count = len(referencing_ids)
        results.append(
            {
                **nc,
                "flow_reference_count": count,
                "referencing_flow_ids": referencing_ids,
                "match_type": match_type if count > 0 else "none",
            }
        )
    return results


def _flow_refs_via_dependency_api(
    named_credentials: List[Dict[str, Any]],
    client: "SalesforceClient",
) -> Optional[Dict[str, List[str]]]:
    """Resolve Flow→NamedCredential references from Salesforce's own dependency
    graph (the ``MetadataComponentDependency`` Tooling object) in ONE query, plus
    one chunked follow-up for indirect Flow→Apex→NamedCredential edges.

    This is complete and fast — it replaces the per-flow ``SELECT Metadata FROM
    Flow WHERE Id = …`` N+1 (the Tooling API forbids multi-row Metadata queries),
    which was O(active_flows) sequential round trips and had to be time-bounded,
    undercounting D5 on large orgs.

    Returns ``{credential_developer_name: [flow_id, …]}`` on success, or ``None``
    if the org does not expose the Dependency API — the caller then falls back to
    the bounded per-flow scan. ``RefMetadataComponentName`` is matched against BOTH
    the credential DeveloperName and MasterLabel, so the mapping is robust to
    whichever form Salesforce reports.
    """
    # Map every name Salesforce might report (dev name OR label) → canonical dev name.
    name_to_dev: Dict[str, str] = {}
    for nc in named_credentials:
        dev = nc.get("credential_developer_name", "")
        if not dev:
            continue
        name_to_dev[dev] = dev
        label = nc.get("credential_name", "")
        if label:
            name_to_dev.setdefault(label, dev)
    if not name_to_dev:
        return {}

    try:
        deps = client.tooling_soql(
            "SELECT MetadataComponentId, MetadataComponentType, "
            "RefMetadataComponentName, RefMetadataComponentType "
            "FROM MetadataComponentDependency "
            "WHERE RefMetadataComponentType = 'NamedCredential'"
        )
    except Exception as e:  # noqa: BLE001 — API not enabled/queryable → fall back.
        print(
            "   MetadataComponentDependency unavailable "
            f"({type(e).__name__}); falling back to the per-flow flow scan."
        )
        return None

    nc_to_flows: Dict[str, set] = {dev: set() for dev in set(name_to_dev.values())}
    apex_to_ncs: Dict[str, set] = {}
    for d in deps:
        dev = name_to_dev.get(d.get("RefMetadataComponentName") or "")
        if not dev:
            continue
        comp_id = d.get("MetadataComponentId")
        if not comp_id:
            continue
        comp_type = d.get("MetadataComponentType")
        if comp_type == "Flow":
            nc_to_flows[dev].add(comp_id)
        elif comp_type == "ApexClass":
            apex_to_ncs.setdefault(comp_id, set()).add(dev)

    # Indirect: Flow → ApexClass → NamedCredential. Chunk the IN() over apex ids
    # to stay well within SOQL limits; best-effort (direct refs already captured).
    apex_ids = list(apex_to_ncs.keys())
    for i in range(0, len(apex_ids), 200):
        in_list = ",".join(f"'{a}'" for a in apex_ids[i : i + 200])
        try:
            apex_deps = client.tooling_soql(
                "SELECT MetadataComponentId, MetadataComponentType, "
                "RefMetadataComponentId, RefMetadataComponentType "
                "FROM MetadataComponentDependency "
                "WHERE MetadataComponentType = 'Flow' "
                "AND RefMetadataComponentType = 'ApexClass' "
                f"AND RefMetadataComponentId IN ({in_list})"
            )
        except Exception:  # noqa: BLE001 — indirect is best-effort.
            continue
        for d in apex_deps:
            flow_id = d.get("MetadataComponentId")
            apex_id = d.get("RefMetadataComponentId")
            if flow_id and apex_id in apex_to_ncs:
                for dev in apex_to_ncs[apex_id]:
                    nc_to_flows[dev].add(flow_id)

    logger.info(
        "Named-credential flow refs via MetadataComponentDependency: "
        "%d reference(s) across %d credential(s)",
        sum(len(v) for v in nc_to_flows.values()),
        sum(1 for v in nc_to_flows.values() if v),
    )
    return {dev: sorted(ids) for dev, ids in nc_to_flows.items()}


def get_named_credential_flow_refs(
    named_credentials: List[Dict[str, Any]],
    client: Optional[SalesforceClient] = None,
) -> List[Dict[str, Any]]:
    """
    Detects both Direct and Indirect references (Flow -> Apex -> Named Credential).

    Resolution order (live mode only — offline returns the catalog unchanged):

      1. **MetadataComponentDependency** (preferred) — one Tooling query returns
         every Flow→NamedCredential reference from Salesforce's own dependency
         graph (plus a chunked follow-up for indirect Flow→Apex→NC edges). No
         per-flow N+1, so the result is COMPLETE and fast; the controls below do
         not apply on this path. Disable with ``SF_DISABLE_DEPENDENCY_API=1`` to
         force the fallback.
      2. **Bounded per-flow scan** (fallback) — runs when the Dependency API is
         not available on the org, is disabled, or reports ZERO references (the
         Beta graph's coverage is not exhaustive, so an all-zero answer is
         corroborated by string-matching rather than trusted outright). Governed
         by the two controls below.

    Performance controls (fallback path only):

      * ``SF_SCAN_APEX_NC_REFS`` (default ``0`` / off): the indirect
        Flow→Apex→NC pass needs every Apex class BODY, an unbounded
        ``SELECT Name, Body FROM ApexClass`` that exceeds the Tooling API
        5000-row cap on large orgs — raising the "Could not scan Apex Classes …
        exceeded 5000 records" warning and then contributing nothing. It is OFF
        by default (no warning, no multi-MB source download); set it to ``1`` to
        enable indirect detection on orgs small enough to scan.
      * ``SF_FLOW_SCAN_BUDGET_SECONDS`` (default ``90``): STEP 2 must fetch each
        active flow's ``Metadata`` one row at a time (the Tooling API forbids
        selecting the compound ``Metadata`` field for more than one record), so
        it is O(active_flows) sequential round trips — tens of minutes on a large
        org. The per-flow scan stops once this wall-clock budget is spent and
        logs how many flows it covered. ``0`` = unlimited (scan every active
        flow, the original behaviour). Undercounting a flow reference only
        matters if it pushes a credential's count below the
        INTEGRATION_CONCENTRATION threshold; raise the budget if that happens.
    """
    if not is_live():
        return named_credentials

    def _env_flag(name: str) -> bool:
        return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")

    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    # --- Preferred path: Salesforce's dependency graph in one query ----------
    # MetadataComponentDependency gives every Flow→NamedCredential reference
    # directly — complete and fast, with no per-flow Metadata N+1 and no time
    # budget. Fall through to the bounded scan below if the org does not expose
    # the Dependency API, if it is explicitly disabled — or if it reports ZERO
    # references: the Dependency API is a Beta whose graph coverage is not
    # exhaustive (e.g. NCs referenced via Apex `callout:` strings or External
    # Credentials may carry no recorded edge), so an all-zero answer is
    # ambiguous and is corroborated by the string-matching scan rather than
    # trusted outright. A true zero then costs one bounded scan and still
    # returns zero; a false zero is caught by whichever references the scan finds.
    if not _env_flag("SF_DISABLE_DEPENDENCY_API"):
        dep_map = _flow_refs_via_dependency_api(named_credentials, client)
        if dep_map is not None:
            if any(dep_map.values()) or not named_credentials:
                return _build_flow_ref_results(
                    named_credentials, dep_map, "dependency_api"
                )
            print(
                "   Dependency graph returned 0 named-credential references — "
                "corroborating with the bounded per-flow scan."
            )

    # --- STEP 1: Scan Apex Classes for Named Credential hardcoding (opt-in) ---
    # We map: { "NC_Developer_Name": ["Class_A", "Class_B"] }
    nc_to_apex_classes: Dict[str, List[str]] = {
        nc["credential_developer_name"]: [] for nc in named_credentials
    }

    if _env_flag("SF_SCAN_APEX_NC_REFS"):
        try:
            # Note: We query Name and Body. Body is where the code is. This is
            # unbounded (no WHERE) and downloads every class' source, so it is
            # gated behind SF_SCAN_APEX_NC_REFS and off by default.
            all_classes = client.tooling_soql("SELECT Name, Body FROM ApexClass")

            for apex in all_classes:
                class_name = apex.get("Name")
                body = apex.get("Body", "")
                if not body:
                    continue

                for nc_name in nc_to_apex_classes.keys():
                    if nc_name in body:
                        nc_to_apex_classes[nc_name].append(class_name)
        except Exception as e:
            print(f"   Warning: Could not scan Apex Classes: {e}")

    # --- STEP 2: Scan Flows for Direct NC references OR Indirect Apex calls ---
    try:
        active_flows = client.tooling_soql(
            "SELECT Id, MasterLabel FROM Flow WHERE Status = 'Active'"
        )
    except Exception as e:
        print(f"Error fetching flows: {e}")
        return named_credentials

    nc_to_flows: Dict[str, List[str]] = {
        nc["credential_developer_name"]: [] for nc in named_credentials
    }

    budget_s = _env_float("SF_FLOW_SCAN_BUDGET_SECONDS", 90.0)
    started = time.monotonic()
    scanned = 0
    total_flows = len(active_flows)

    for flow in active_flows:
        # Bound the per-flow Metadata N+1: stop once the wall-clock budget is
        # spent so a large org does not stall the whole run for tens of minutes.
        if budget_s > 0 and (time.monotonic() - started) >= budget_s:
            print(
                f"   Flow NC-reference scan hit its {budget_s:.0f}s budget after "
                f"{scanned}/{total_flows} active flows; skipping the remaining "
                f"{total_flows - scanned}. Set SF_FLOW_SCAN_BUDGET_SECONDS=0 to "
                f"scan all (slower)."
            )
            break

        flow_id = flow["Id"]
        flow_label = flow.get("MasterLabel", "Unknown")
        scanned += 1

        try:
            meta_recs = client.tooling_soql(
                f"SELECT Metadata FROM Flow WHERE Id = '{flow_id}'"
            )
            if not meta_recs or not meta_recs[0].get("Metadata"):
                continue

            metadata_str = json.dumps(meta_recs[0]["Metadata"])

            for nc_dev_name in nc_to_flows.keys():
                # Check A: Direct reference
                direct_found = nc_dev_name in metadata_str

                # Check B: Indirect reference via any Apex Class found in Step 1
                suspect_classes = nc_to_apex_classes.get(nc_dev_name, [])
                indirect_found = any(
                    cls_name in metadata_str for cls_name in suspect_classes
                )

                if direct_found or indirect_found:
                    nc_to_flows[nc_dev_name].append(flow_id)
                    reason = (
                        "DIRECT"
                        if direct_found
                        else f"INDIRECT via Apex ({[c for c in suspect_classes if c in metadata_str]})"
                    )

        except Exception as e:
            print(f"   Skipping Flow {flow_label}: {e}")

    # --- STEP 3: Build Payload ---
    results = []
    for nc in named_credentials:
        dev_name = nc.get("credential_developer_name", "")
        referencing_ids = nc_to_flows.get(dev_name, [])
        count = len(referencing_ids)

        results.append(
            {
                **nc,
                "flow_reference_count": count,
                "referencing_flow_ids": referencing_ids,
                "match_type": "apex_flow_trace_scan" if count > 0 else "none",
            }
        )

    return results


def get_permission_bottlenecks(
    client: Optional[SalesforceClient] = None,
) -> List[Dict[str, Any]]:
    """
    Alias for get_approval_pending — D6 uses the same data as D3.
    The bottleneck_score field is the primary D6 signal.

    Returns: same shape as get_approval_pending()
    """
    return get_approval_pending(client)


def get_cross_system_references(
    client: Optional[SalesforceClient] = None,
    patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Detect Cases containing external ticket patterns (INC-, JIRA-, CS-, etc.)

    SME SOQL query:
        -- Cases with INC- references (run once per pattern)
        SELECT COUNT(Id) FROM Case
        WHERE Subject LIKE '%INC-%'
        AND CreatedDate = LAST_N_DAYS:90

        -- Sample matches for evidence
        SELECT Id, Subject FROM Case
        WHERE Subject LIKE '%INC-%'
        AND CreatedDate = LAST_N_DAYS:90 LIMIT 20

    Returns: cross_system_references dict matching salesforce_sample.json shape
    """
    if not is_live():
        return _load_fixture()["cross_system_references"]

    if patterns is None:
        patterns = ["INC-", "JIRA-"]

    total_recs = client.soql(
        "SELECT COUNT(Id) FROM Case WHERE CreatedDate = LAST_N_DAYS:90"
    )
    total_cases = total_recs[0].get("expr0", 0) if total_recs else 0

    echo_count = 0
    sample_matches = []
    matched_patterns = []

    for pattern in patterns:
        like = f"%{pattern}%"
        cnt_recs = client.soql(
            f"SELECT COUNT(Id) FROM Case WHERE "
            f"Subject LIKE '{like}' "
            f"AND CreatedDate = LAST_N_DAYS:90"
        )
        cnt = cnt_recs[0].get("expr0", 0) if cnt_recs else 0
        if cnt > 0:
            matched_patterns.append(pattern)
            echo_count += cnt
            # Sample matches for evidence snippet
            sample_recs = client.soql(
                f"SELECT Id, Subject FROM Case WHERE "
                f"Subject LIKE '{like}' "
                f"AND CreatedDate = LAST_N_DAYS:90 LIMIT 5"
            )
            for r in sample_recs:
                sample_matches.append(
                    {
                        "case_id": r.get("Id", ""),
                        "pattern": pattern,
                        "field": "Subject",
                    }
                )

    sf_echo_score = round(echo_count / total_cases, 4) if total_cases > 0 else 0.0

    return {
        "sf_echo_count": echo_count,
        "sf_total_cases": total_cases,
        "sf_echo_score": sf_echo_score,
        "matched_patterns": matched_patterns,
        "sample_matches": sample_matches,
    }


def _collect_owner_ids(
    approval_processes: List[Dict[str, Any]],
    cases: List[Dict[str, Any]],
) -> List[str]:
    """Gather every distinct owner/approver User Id referenced by a run's data.

    Mirrors the fields entity extraction reads: approver Ids on approval
    processes and OwnerId/AssignedTo on Case records. Only string-shaped values
    are collected; dict-shaped owner references already carry their own name.
    """
    ids: set[str] = set()
    for proc in approval_processes or []:
        if not isinstance(proc, dict):
            continue
        for approver_id in proc.get("approver_ids") or []:
            if isinstance(approver_id, str) and approver_id.strip():
                ids.add(approver_id.strip())
    for record in cases or []:
        if not isinstance(record, dict):
            continue
        for field_name in ("OwnerId", "owner_id", "AssignedTo", "assigned_to"):
            val = record.get(field_name)
            if isinstance(val, str) and val.strip():
                ids.add(val.strip())
    return list(ids)


def resolve_user_names(
    query_fn: Any,
    user_ids: List[str],
) -> Dict[str, str]:
    """Resolve Salesforce User Ids to display Names via a batched SOQL query.

    Owner/approver fields on Salesforce records carry the raw User Id
    (e.g. ``005xx000001AAA1``), not a human name. Entity extraction stores the
    ``display_name`` from these fields, so without a lookup the knowledge graph
    surfaces the raw Id to the user. This resolves every distinct Id referenced
    in a run with batched ``SELECT Id, Name FROM User WHERE Id IN (...)`` queries
    (chunked to stay within SOQL limits), never one query per owner.

    ``query_fn`` is any callable that runs a SOQL string and returns the record
    list — ``SalesforceClient.soql`` and ``NcinoClient.query`` both fit, so the
    Salesforce and nCino ingestors share this logic against their own org client.

    The returned map is keyed by both the 18-char and 15-char forms of each Id
    so callers can look up either. It is best-effort: empty input or a failed
    query yields ``{}`` and the caller falls back to the raw Id (current
    behavior) — the run never breaks.
    """
    ids = sorted({uid.strip() for uid in user_ids if isinstance(uid, str) and uid.strip()})
    if not ids or query_fn is None:
        return {}

    names: Dict[str, str] = {}
    CHUNK = 200  # keep the IN (...) clause comfortably within SOQL length limits
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        # Ids are Salesforce key-prefixed alphanumerics; quote defensively anyway.
        in_clause = ", ".join("'" + cid.replace("'", "") + "'" for cid in chunk)
        try:
            recs = query_fn(f"SELECT Id, Name FROM User WHERE Id IN ({in_clause})")
        except Exception as exc:
            # Graceful degradation: a lookup failure must not break the run.
            logger.warning("User name resolution failed (non-blocking): %s", exc)
            continue
        for r in recs or []:
            uid = r.get("Id")
            name = r.get("Name")
            if uid and name:
                names[uid] = name
                # Alias the 15-char case-sensitive form so either width resolves.
                if len(uid) == 18:
                    names[uid[:15]] = name
    return names


def get_user_names(
    client: Optional[SalesforceClient],
    user_ids: List[str],
) -> Dict[str, str]:
    """Live-mode wrapper around :func:`resolve_user_names` for Salesforce.

    Returns ``{}`` in offline mode or when no client is available, so offline
    runs stay deterministic and the caller falls back to the raw Id.
    """
    if not is_live() or client is None:
        return {}
    return resolve_user_names(client.soql, user_ids)


def get_relationship_records(
    client: Optional[SalesforceClient] = None,
) -> List[Dict[str, Any]]:
    """Return a bounded set of source-backed Case ownership records.

    Sprint 13 relationship mapping needs the record Id and OwnerId, not only
    aggregate case metrics. Keeping this as a separate bounded query preserves
    the existing detector payload while making Person -> Object ``owns`` edges
    possible in live Service Cloud runs.
    """
    if not is_live():
        fixture = _load_fixture()
        return list(fixture.get("cases") or fixture.get("records") or [])

    return client.soql(
        "SELECT Id, CaseNumber, Subject, OwnerId, Status, CreatedDate "
        "FROM Case WHERE CreatedDate = LAST_N_DAYS:90 "
        "ORDER BY CreatedDate DESC LIMIT 500"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main ingest() — called by runner.py
# ─────────────────────────────────────────────────────────────────────────────


def ingest(sf_client: Optional[SalesforceClient] = None) -> Dict[str, Any]:
    """
    Orchestrate all seven ingestion functions and return a single payload.
    Offline: reads fixtures. Live: calls all seven functions against the org.

    Returns: dict with keys matching salesforce_sample.json top-level keys.
    """
    if not is_live():
        logger.info("Salesforce ingestion: offline mode (fixture)")
        return _load_fixture()

    logger.info("Salesforce ingestion: live mode")
    if sf_client is None:
        sf_client = _get_client()

    try:

        def _timed(fn_name, fn_call):
            """Execute an ingestion function with timing and governor limit logging."""
            t0 = time.perf_counter()
            try:
                result = fn_call()
                elapsed = int((time.perf_counter() - t0) * 1000)
                rows = (
                    len(result)
                    if isinstance(result, list)
                    else result.get(
                        "total_cases_90d",
                        result.get(
                            "active_flow_count_on_object",
                            result.get(
                                "sf_total_cases",
                                len(result) if isinstance(result, dict) else 0,
                            ),
                        ),
                    )
                )
                logger.info(
                    f"INFO[{fn_name}]{'':>3} rows={rows:<6} ms={elapsed:<6} status=OK"
                )
                return result
            except IngestError as e:
                elapsed = int((time.perf_counter() - t0) * 1000)
                logger.error(f"ERROR [{fn_name}]{'':>3} ms={elapsed:<6} {str(e)[:120]}")
                raise

        case_metrics = _timed("get_case_metrics", lambda: get_case_metrics(sf_client))
        flow_inventory = _timed(
            "get_flow_inventory", lambda: get_flow_inventory(sf_client)
        )
        approval_processes = _timed(
            "get_approval_pending", lambda: get_approval_pending(sf_client)
        )
        named_credentials_catalog = _timed(
            "get_named_credentials", lambda: get_named_credentials(sf_client)
        )
        named_credentials = _timed(
            "get_named_credential_flow_refs",
            lambda: get_named_credential_flow_refs(
                named_credentials_catalog, sf_client
            ),
        )
        cross_system_references = _timed(
            "get_cross_system_references",
            lambda: get_cross_system_references(sf_client),
        )
        cases = _timed(
            "get_relationship_records",
            lambda: get_relationship_records(sf_client),
        )

        # Resolve owner/approver User Ids to display names once per run. Owner
        # and approver fields carry raw User Ids; entity extraction reads this
        # map so the knowledge graph shows real names instead of raw Ids. The
        # lookup is batched and best-effort — on failure user_names is empty and
        # extraction falls back to the raw Id.
        owner_ids = _collect_owner_ids(approval_processes, cases)
        user_names = get_user_names(sf_client, owner_ids)
        if user_names:
            logger.info(
                "Resolved %d Salesforce owner/approver Id(s) to display names",
                len({v for v in user_names.values()}),
            )

        return {
            "case_metrics": case_metrics,
            "flow_inventory": flow_inventory,
            "approval_processes": approval_processes,
            "named_credentials": named_credentials,
            "cross_system_references": cross_system_references,
            "cases": cases,
            "user_names": user_names,
        }
    except CredentialRecordError:
        raise
    except IngestError:
        raise
    except Exception as e:
        raise IngestError(f"Salesforce ingestion failed unexpectedly: {e}") from e
