"""
SF-2.2 — Salesforce Ingestion Module

Offline mode: reads backend/discovery/ingest/fixtures/salesforce_sample.json
Live mode:    calls Salesforce REST + SOQL + Tooling APIs

Environment variables for live mode:
    SF_INSTANCE_URL   e.g. https://myorg.my.salesforce.com
    SF_ACCESS_TOKEN   OAuth access token

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

from . import is_live

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

def _get_client() -> "SalesforceClient":
    """Build a minimal REST client from env vars."""
    instance_url = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
    access_token = os.getenv("SF_ACCESS_TOKEN", "")
    if not instance_url or not access_token:
        raise IngestError(
            "Live mode requires SF_INSTANCE_URL and SF_ACCESS_TOKEN environment variables. "
            "Set INGEST_MODE=offline to run without credentials."
        )
    return SalesforceClient(instance_url, access_token)


class SalesforceClient:
    """Thin wrapper around Salesforce REST APIs."""

    def __init__(self, instance_url: str, access_token: str):
        self.instance_url = instance_url
        self.access_token = access_token
        self._session = None

    def _session_get(self):
        try:
            import requests
            if self._session is None:
                self._session = requests.Session()
                self._session.headers.update({
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                })
            return self._session
        except ImportError:
            raise IngestError("requests library required for live mode: pip install requests")

    def soql(self, query: str) -> List[Dict]:
        """Execute a SOQL query. Returns records list."""
        import urllib.parse
        session = self._session_get()
        url = f"{self.instance_url}/services/data/{API_VERSION}/query/"
        params = {"q": query}
        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            records = data.get("records", [])
            # Handle pagination
            next_url = data.get("nextRecordsUrl")
            while next_url:
                resp2 = session.get(f"{self.instance_url}{next_url}", timeout=30)
                resp2.raise_for_status()
                page = resp2.json()
                records.extend(page.get("records", []))
                next_url = page.get("nextRecordsUrl")
            return records
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
            resp.raise_for_status()
            data = resp.json()
            records = data.get("records", [])
            next_url = data.get("nextRecordsUrl")
            while next_url:
                if len(records) >= max_records:
                    raise IngestError(
                        f"Tooling API result exceeded {max_records} records. "
                        f"Add a WHERE clause to narrow the query."
                    )
                resp2 = session.get(
                    f"{self.instance_url}{next_url}", timeout=60
                )
                resp2.raise_for_status()
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
        SELECT COUNT(DISTINCT CaseId) linked FROM CaseArticle
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
        "SELECT COUNT(DISTINCT CaseId) FROM CaseArticle "
        "WHERE CreatedDate = LAST_N_DAYS:90"
    )
    cases_with_kb = kb_recs[0].get("expr0", 0) if kb_recs else 0

    handoff_score = round(owner_changes / total_cases, 4) if total_cases > 0 else 0.0
    knowledge_gap_score = round(1 - (cases_with_kb / closed_cases), 4) if closed_cases > 0 else 0.0

    # Category breakdown
    cat_recs = client.soql(
        "SELECT Reason, COUNT(Id) FROM Case "
        "WHERE CreatedDate = LAST_N_DAYS:90 GROUP BY Reason"
    )
    category_breakdown = [
        {"category": r.get("Reason", "Unknown"), "volume": r.get("expr0", 0),
         "handoff_score": 0.0, "avg_age_days": 0.0}
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
    Pull active AutoLaunchedFlows on high-volume objects via Tooling API.

    SME Tooling API queries:
        -- Active flows
        SELECT Id, MasterLabel, ProcessType, TriggerType,
               TriggerObjectOrEventLabel, Status
        FROM FlowVersionView WHERE Status = 'Active'

        -- Element count from Flow Metadata (heavy — paginate)
        SELECT Id, MasterLabel, Metadata FROM Flow WHERE Status = 'Active'

    Returns: flow_inventory dict matching salesforce_sample.json shape
    """
    if not is_live():
        return _load_fixture()["flow_inventory"]

    flow_recs = client.tooling_soql(
        "SELECT Id, MasterLabel, ProcessType, TriggerType, "
        "TriggerObjectOrEventLabel, Status FROM FlowVersionView "
        "WHERE Status = 'Active'"
    )

    auto_launched = [
        r for r in flow_recs
        if r.get("ProcessType") == "AutoLaunchedFlow"
        and r.get("TriggerObjectOrEventLabel") == "Case"
    ]

    # Element counts from Metadata (best-effort; may be slow on large orgs)
    element_counts = []
    for r in auto_launched:
        try:
            meta_recs = client.tooling_soql(
                f"SELECT Id, MasterLabel, Metadata FROM Flow "
                f"WHERE Id = '{r['Id']}'"
            )
            if meta_recs and meta_recs[0].get("Metadata"):
                meta = meta_recs[0]["Metadata"]
                # Count all element arrays in flow metadata
                count = sum(
                    len(meta.get(k, []))
                    for k in ["decisions", "loops", "recordCreates",
                               "recordDeletes", "recordLookups", "recordUpdates",
                               "assignments", "subflows", "actionCalls"]
                )
                element_counts.append(count)
        except Exception:
            element_counts.append(0)

    avg_elements = round(sum(element_counts) / len(element_counts), 2) if element_counts else 0.0
    records_90d = _load_fixture()["case_metrics"].get("total_cases_90d", 0)  # from case query
    flow_activity_score = round(
        (records_90d / 90) * (len(auto_launched) / max(avg_elements, 1)), 4
    ) if auto_launched else 0.0

    return {
        "active_flow_count_on_object": len(auto_launched),
        "avg_element_count": avg_elements,
        "flow_activity_score": flow_activity_score,
        "trigger_object": "Case",
        "records_90d": records_90d,
        "flows": [
            {
                "flow_id": r["Id"],
                "flow_label": r["MasterLabel"],
                "process_type": r["ProcessType"],
                "element_count": element_counts[i] if i < len(element_counts) else 0,
                "trigger_object": r.get("TriggerObjectOrEventLabel", ""),
            }
            for i, r in enumerate(auto_launched)
        ],
    }


def get_approval_pending(client: Optional[SalesforceClient] = None) -> List[Dict[str, Any]]:
    """
    Pull pending ProcessInstance records with step age and approver count.

    SME SOQL queries:
        -- Pending approvals
        SELECT ProcessDefinition.Name, Status, CreatedDate
        FROM ProcessInstance WHERE Status = 'Pending' LIMIT 1000

        -- Approvers per pending instance
        SELECT ProcessInstanceId, ActorId, StepStatus
        FROM ProcessInstanceWorkitem WHERE StepStatus = 'Pending'

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
                "process_name": name, "pending_count": 0,
                "total_age_days": 0.0, "pi_ids": [],
            }
        by_process[name]["pending_count"] += 1
        by_process[name]["total_age_days"] += age_days
        by_process[name]["pi_ids"].append(pi_id)

    # ProcessInstanceWorkitem: ActorId can be User, Role, Queue, or Group.
    # approver_count = distinct ActorId values — may undercount human capacity
    # when Roles/Queues are used. approver_type_notes flags this explicitly.
    wi_recs = client.soql(
        "SELECT ProcessInstanceId, ActorId, Actor.Type, StepStatus "
        "FROM ProcessInstanceWorkitem WHERE StepStatus = 'Pending'"
    )

    # Map: process_name -> {actor_id -> actor_type}
    pi_to_process = {
        pi_id: name
        for name, info in by_process.items()
        for pi_id in info["pi_ids"]
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

        bottleneck_score = round(cnt / approver_count, 2) if approver_count > 0 else float(cnt)

        results.append({
            "process_name": name,
            "pending_count": cnt,
            "avg_delay_days": avg_delay,
            "approver_count": approver_count,
            "bottleneck_score": bottleneck_score,
            "approver_ids": list(actors.keys()),
            "approver_type_breakdown": type_counts,
            "approver_type_notes": approver_type_notes,
        })

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


def get_named_credentials(client: Optional[SalesforceClient] = None) -> List[Dict[str, Any]]:
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
    ("actionCalls", "connector"),            # actionCalls[*].connector = devName
    ("actionCalls", "namedCredential"),      # alternative field name used in some API versions
    # Apex actions that receive Named Credential as input variable
    ("apexPluginCalls", "namedCredential"),
    # ExternalService-backed action calls
    ("externalServiceActions", "namedCredential"),
]

# False-positive guard: these strings appear in many flows and are NOT credentials.
_NC_FALSE_POSITIVE_TOKENS = {
    "null", "true", "false", "Id", "Name", "Status", "OwnerId",
    "CreatedDate", "LastModifiedDate", "IsActive",
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


def get_named_credential_flow_refs(
    named_credentials: List[Dict[str, Any]],
    client: Optional[SalesforceClient] = None,
) -> List[Dict[str, Any]]:
    """
    Scan active Flow.Metadata for references to each Named Credential (D5 signal).

    DETECTION STRATEGY — v1 field-level inspection (NOT full-JSON string scan):
    Rather than serialising the entire Metadata blob and string-searching it
    (which produces false positives from DeveloperNames in unrelated fields),
    this function inspects only the known Salesforce Metadata sub-fields where
    Named Credential references actually live:

        actionCalls[*].connector
        actionCalls[*].namedCredential
        apexPluginCalls[*].namedCredential
        externalServiceActions[*].namedCredential

    Match types returned (stored in match_type field):
        "field_exact"  — dev_name matched in a known field (highest confidence)
        "label_field"  — MasterLabel matched in a known field (medium confidence)
        "none"         — no match found

    KNOWN LIMITATIONS (documented explicitly so SF-3.2 can extend):
        - Apex actions that build credential names dynamically at runtime
          cannot be detected statically.
        - Platform Event triggered flows that reference credentials indirectly
          will be missed.
        - Named Credentials referenced only in Screen Flow HTTP actions
          (not AutoLaunchedFlow) are included — they inflate D5 if present.
        - Managed package flows with null Metadata are skipped silently and
          logged at DEBUG level.
        - The _NC_FIELD_PATHS list is fixed for Salesforce API v59.0 — later
          API versions may add new field paths. Review at each API version bump.

    In offline mode: returns named_credentials list unchanged (fixture already
    contains flow_reference_count and referencing_flow_ids).

    SME Tooling API query:
        SELECT Id, MasterLabel, Metadata FROM Flow WHERE Status = 'Active'
        (paginated — large orgs may have 500+ active flows)
    """
    if not is_live():
        return named_credentials

    # Paginated fetch — Tooling API also supports nextRecordsUrl
    flow_meta: List[Dict] = []
    url = (
        f"{client.instance_url}/services/data/{API_VERSION}/tooling/query/"
        f"?q=SELECT+Id,MasterLabel,Metadata+FROM+Flow+WHERE+Status='Active'"
    )
    session = client._session_get()
    while url:
        try:
            resp = session.get(url, timeout=60)
            resp.raise_for_status()
            page = resp.json()
            flow_meta.extend(page.get("records", []))
            next_rel = page.get("nextRecordsUrl")
            url = f"{client.instance_url}{next_rel}" if next_rel else None
        except Exception as e:
            raise IngestError(f"Flow metadata fetch failed: {e}")

    logger.info(f"Flow metadata: {len(flow_meta)} active flows fetched for credential scan")

    results = []
    for nc in named_credentials:
        dev_name = nc.get("credential_developer_name", "")
        label = nc.get("credential_name", "")
        referencing_ids: List[str] = []
        match_types: List[str] = []

        for fm in flow_meta:
            metadata = fm.get("Metadata")
            if not metadata:
                logger.debug(f"Flow {fm.get('Id')} has null Metadata — skipped in NC scan")
                continue
            mtype = _flow_references_credential(metadata, dev_name, label)
            if mtype:
                referencing_ids.append(fm["Id"])
                match_types.append(mtype)

        # Dominant match type: field_exact > label_field > none
        dominant = "field_exact" if "field_exact" in match_types else                    "label_field" if "label_field" in match_types else "none"

        results.append({
            **nc,
            "flow_reference_count": len(referencing_ids),
            "referencing_flow_ids": referencing_ids,
            "match_type": dominant,
        })

    return results


def get_permission_bottlenecks(client: Optional[SalesforceClient] = None) -> List[Dict[str, Any]]:
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
        WHERE (Subject LIKE '%INC-%' OR Description LIKE '%INC-%')
        AND CreatedDate = LAST_N_DAYS:90

        -- Sample matches for evidence
        SELECT Id, Subject, Description FROM Case
        WHERE (Subject LIKE '%INC-%' OR Description LIKE '%INC-%')
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
            f"(Subject LIKE '{like}' OR Description LIKE '{like}') "
            f"AND CreatedDate = LAST_N_DAYS:90"
        )
        cnt = cnt_recs[0].get("expr0", 0) if cnt_recs else 0
        if cnt > 0:
            matched_patterns.append(pattern)
            echo_count += cnt
            # Sample matches for evidence snippet
            sample_recs = client.soql(
                f"SELECT Id, Subject FROM Case WHERE "
                f"(Subject LIKE '{like}' OR Description LIKE '{like}') "
                f"AND CreatedDate = LAST_N_DAYS:90 LIMIT 5"
            )
            for r in sample_recs:
                sample_matches.append({
                    "case_id": r.get("Id", ""),
                    "pattern": pattern,
                    "field": "Subject" if pattern in r.get("Subject", "") else "Description",
                })

    sf_echo_score = round(echo_count / total_cases, 4) if total_cases > 0 else 0.0

    return {
        "sf_echo_count": echo_count,
        "sf_total_cases": total_cases,
        "sf_echo_score": sf_echo_score,
        "matched_patterns": matched_patterns,
        "sample_matches": sample_matches,
    }


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
                    len(result) if isinstance(result, list)
                    else result.get("total_cases_90d",
                         result.get("active_flow_count_on_object",
                         result.get("sf_total_cases", len(result) if isinstance(result, dict) else 0)))
                )
                logger.info(
                    f"INFO  [{fn_name}]{'':>3} rows={rows:<6} ms={elapsed:<6} status=OK"
                )
                return result
            except IngestError as e:
                elapsed = int((time.perf_counter() - t0) * 1000)
                logger.error(
                    f"ERROR [{fn_name}]{'':>3} ms={elapsed:<6} {str(e)[:120]}"
                )
                raise

        case_metrics              = _timed("get_case_metrics",              lambda: get_case_metrics(sf_client))
        flow_inventory            = _timed("get_flow_inventory",            lambda: get_flow_inventory(sf_client))
        approval_processes        = _timed("get_approval_pending",          lambda: get_approval_pending(sf_client))
        named_credentials_catalog = _timed("get_named_credentials",         lambda: get_named_credentials(sf_client))
        named_credentials         = _timed("get_named_credential_flow_refs",lambda: get_named_credential_flow_refs(named_credentials_catalog, sf_client))
        cross_system_references   = _timed("get_cross_system_references",   lambda: get_cross_system_references(sf_client))

        return {
            "case_metrics":           case_metrics,
            "flow_inventory":         flow_inventory,
            "approval_processes":     approval_processes,
            "named_credentials":      named_credentials,
            "cross_system_references":cross_system_references,
        }
    except IngestError:
        raise
    except Exception as e:
        raise IngestError(f"Salesforce ingestion failed unexpectedly: {e}") from e
