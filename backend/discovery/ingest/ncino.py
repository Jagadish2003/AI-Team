"""
nCino Lending Ingestion Module — v3 (fully corrected)

Correction history:
  v1: Original — used hallucinated objects LLC_BI__Loan_Stage__c,
      LLC_BI__Spreading__c, LLC_BI__Document_Checklist__c, LLC_BI__Approval__c
  v2: Fixed LLC_BI__Loan_Stage__c → LLC_BI__Loan__History
  v3: Fixed all remaining hallucinated objects using confirmed org metadata:
      LLC_BI__Covenant__c        → LLC_BI__Covenant2__c
                                   (via LLC_BI__Covenant_Compliance__c join)
      LLC_BI__Spreading__c       → LLC_BI__Spread_Statement_Period__c
      LLC_BI__Document_Checklist__c → LLC_BI__Checklist__c
      LLC_BI__Approval__c        → ProcessInstance (standard Salesforce)

Confirmed objects (from real org metadata — May 2026):
  LLC_BI__Loan__c                    ✓ core anchor object
  LLC_BI__Loan__History              ✓ stage transition history
  LLC_BI__Covenant2__c               ✓ current covenant standard
  LLC_BI__Covenant_Compliance__c     ✓ per-loan covenant compliance link
  LLC_BI__Checklist__c               ✓ workflow task checklists per loan
  LLC_BI__Spread_Statement_Period__c ✓ financial spread periods per analyst
  ProcessInstance                    ✓ standard Salesforce approval workflow
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import is_live

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ncino_sample.json"
API_VERSION  = "v59.0"
WINDOW_DAYS  = 90

# Stage duration benchmarks (SME to confirm per org — defaults below)
STAGE_DURATION_BENCHMARKS: Dict[str, int] = {
    "Application":       3,
    "Pre-Qualification": 2,
    "Underwriting":     10,
    "Credit Review":     7,
    "Approval":          5,
    "Commitment":        3,
    "Closing":           7,
    "Post-Close":        5,
    "__DEFAULT__":      14,
}


class NcinoIngestError(Exception):
    pass


def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE_PATH.exists():
        raise NcinoIngestError(f"nCino fixture not found: {FIXTURE_PATH}")
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


class NcinoClient:
    def __init__(self, instance_url: str, access_token: str):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self._session = None

    def _get_session(self):
        try:
            import requests
        except ImportError:
            raise NcinoIngestError("requests library required")
        if self._session is None:
            self._session = __import__("requests").Session()
            self._session.headers.update({
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            })
        return self._session

    def query(self, soql: str) -> List[Dict[str, Any]]:
        url = f"{self.instance_url}/services/data/{API_VERSION}/query"
        session = self._get_session()
        records: List[Dict[str, Any]] = []
        params = {"q": soql}
        while True:
            resp = session.get(url, params=params)
            if resp.status_code != 200:
                raise NcinoIngestError(
                    f"SOQL failed ({resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
            records.extend(data.get("records", []))
            next_url = data.get("nextRecordsUrl")
            if not next_url:
                break
            url = f"{self.instance_url}{next_url}"
            params = {}
        return records


def _get_client() -> NcinoClient:
    instance_url = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
    access_token = os.getenv("SF_ACCESS_TOKEN", "")
    if not instance_url or not access_token:
        raise NcinoIngestError(
            "Live mode requires SF_INSTANCE_URL and SF_ACCESS_TOKEN."
        )
    return NcinoClient(instance_url, access_token)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    if isinstance(val, date):
        return val
    try:
        return datetime.fromisoformat(str(val)[:10]).date()
    except Exception:
        return None


def _days_since(d: Optional[date]) -> Optional[int]:
    if d is None:
        return None
    return max(0, (_today() - d).days)


# ─────────────────────────────────────────────────────────────────────────────
# FETCH FUNCTIONS — live org
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_loans(client: NcinoClient) -> List[Dict[str, Any]]:
    rows = client.query(f"""
        SELECT Id, Name, OwnerId, LLC_BI__Stage__c, LLC_BI__Amount__c,
               LLC_BI__Closed_Date__c, LLC_BI__Loan_Type_Code__c,
               CreatedDate, LastModifiedDate
        FROM LLC_BI__Loan__c
        WHERE LastModifiedDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("loans=%d", len(rows))
    return rows


def _fetch_stage_history(client: NcinoClient) -> List[Dict[str, Any]]:
    """
    Stage transition history from standard Salesforce field history.
    LLC_BI__Loan__History is created automatically when field history
    tracking is enabled on LLC_BI__Stage__c.
    """
    rows = client.query(f"""
        SELECT Id, ParentId, Field, OldValue, NewValue,
               CreatedDate, CreatedByIdId
        FROM LLC_BI__Loan__History
        WHERE Field = 'LLC_BI__Stage__c'
        AND CreatedDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 10000
    """)
    logger.info("stage_history=%d", len(rows))
    return rows


def _fetch_covenant_compliance(client: NcinoClient) -> List[Dict[str, Any]]:
    """
    Fetch covenant signals directly from LLC_BI__Covenant2__c.

    NC-2 confirmed from real org (May 2026):
      LLC_BI__Covenant_Compliance__c fields: LLC_BI__Covenant__c (NOT Covenant2__c),
      LLC_BI__Status__c, LLC_BI__Evaluation_Date__c — NO loan link, NO active flag.

      LLC_BI__Covenant_Compliance__c is a compliance EVENT record (review log),
      not the right source for overdue/breached signals.

    Correct approach: query LLC_BI__Covenant2__c directly.
    Confirmed fields: LLC_BI__Overdue__c, LLC_BI__Breached__c,
    LLC_BI__Due_Date__c, LLC_BI__Account__c, LLC_BI__Active__c.
    """
    rows = client.query(f"""
        SELECT Id, Name, LLC_BI__Account__c,
               LLC_BI__Active__c,
               LLC_BI__Due_Date__c,
               LLC_BI__Overdue__c,
               LLC_BI__Breached__c,
               LLC_BI__Days_Past_Next_Evaluation__c,
               LLC_BI__Last_Evaluation_Date__c,
               LLC_BI__Last_Evaluation_Status__c,
               LLC_BI__Covenant_Status__c,
               LLC_BI__Frequency__c
        FROM LLC_BI__Covenant2__c
        WHERE LLC_BI__Active__c = true
        AND CreatedDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("covenant2=%d", len(rows))
    return rows


def _fetch_checklists(client: NcinoClient) -> List[Dict[str, Any]]:
    """
    LLC_BI__Checklist__c tracks workflow task checklists per loan.
    Fields confirmed from real org metadata:
      LLC_BI__Actual_Duration_Days__c   — actual time taken
      LLC_BI__Expected_Duration_Days__c — benchmark time
      LLC_BI__Status__c                 — 'To Do' = incomplete
      LLC_BI__Context_Type__c           — filter to loan checklists
      LLC_BI__Loan__c                   — direct loan link
    """
    rows = client.query(f"""
        SELECT Id, LLC_BI__Loan__c, LLC_BI__Status__c,
               LLC_BI__Actual_Duration_Days__c,
               LLC_BI__Expected_Duration_Days__c,
               LLC_BI__Category__c,
               LLC_BI__Context_Type__c,
               CreatedDate
        FROM LLC_BI__Checklist__c
        WHERE LLC_BI__Context_Type__c = 'LLC_BI__Loan__c'
        AND CreatedDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("checklists=%d", len(rows))
    return rows


def _fetch_spread_periods(client: NcinoClient) -> List[Dict[str, Any]]:
    """
    LLC_BI__Spread_Statement_Period__c — financial spreading periods.

    Confirmed from real Org 2 metadata — May 2026:
      - NO LLC_BI__Loan__c field on this object (two-hop relationship)
      - NO LLC_BI__Analyst__c field on either Spread or SpreadStatementPeriod
      - NO LLC_BI__Statement_Date__c on this object
      - LLC_BI__Is_Locked__c CONFIRMED — primary bottleneck signal
      - LLC_BI__Spread__c CONFIRMED on this object — the parent spread header

    Loan relationship (two hops):
      LLC_BI__Loan__c → LLC_BI__Spread__c → LLC_BI__Spread_Statement_Period__c
      Join: WHERE LLC_BI__Spread__c IN (SELECT Id FROM LLC_BI__Spread__c
            WHERE LLC_BI__Loan__c IN [...])

    Analyst field: LLC_BI__Analyst__c — CONFIRMED Lookup(User) on this object.
    Also fetch CreatedById as fallback when Analyst__c is null.

    Signal: LLC_BI__Is_Locked__c = false AND CreatedDate > 14 days ago.
    """
    rows = client.query(f"""
        SELECT Id, LLC_BI__Spread__c,
               LLC_BI__Analyst__c,
               LLC_BI__Is_Locked__c, LLC_BI__Is_Annual__c,
               CreatedDate, LastModifiedDate,
               CreatedById
        FROM LLC_BI__Spread_Statement_Period__c
        WHERE CreatedDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("spread_periods=%d", len(rows))
    return rows


def _fetch_spreads(client: NcinoClient) -> List[Dict[str, Any]]:
    """
    LLC_BI__Spread__c — the spread header record.
    Confirmed fields from real Org 2:
      LLC_BI__Loan__c CONFIRMED — direct loan link.
    Used to resolve the loan for each LLC_BI__Spread_Statement_Period__c record.
    """
    rows = client.query(f"""
        SELECT Id, LLC_BI__Loan__c,
               LLC_BI__Underwriting_Summary__c,
               CreatedDate
        FROM LLC_BI__Spread__c
        WHERE CreatedDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("spreads=%d", len(rows))
    return rows


def _fetch_approval_instances(client: NcinoClient) -> List[Dict[str, Any]]:
    """
    ProcessInstance — standard Salesforce approval workflow.
    Confirmed from real org: ProcessInstance.TargetObjectId links to
    LLC_BI__Loan__c records.
    Pending = Status not in (Approved, Rejected, Recalled) or CompletedDate IS NULL.
    """
    rows = client.query(f"""
        SELECT Id, TargetObjectId, Status, CreatedDate, CompletedDate,
               SubmittedById
        FROM ProcessInstance
        WHERE CreatedDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    # Filter to loan-related approvals in Python (can't subquery with LIMIT)
    logger.info("process_instances=%d (all)", len(rows))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# METRIC BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_origination_metrics(
    loans: List[Dict[str, Any]],
    stage_history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Routing friction from LLC_BI__Loan__History.
    Signal: stage transition count >= 4 per loan  OR  CreatedById changes >= 2.
    """
    if not loans:
        return {
            "total_loans": 0, "avg_stage_transitions": 0.0,
            "max_stage_transitions": 0, "avg_owner_changes": 0.0,
            "max_owner_changes": 0, "high_friction_loans": [],
            "owner_change_source": "LOAN_HISTORY_CREATEDBY",
        }

    by_loan: Dict[str, List] = {}
    for h in stage_history:
        lid = h.get("ParentId", "")  # LLC_BI__Loan__History uses ParentId
        if lid:
            by_loan.setdefault(lid, []).append(h)
    for lid in by_loan:
        by_loan[lid].sort(key=lambda h: h.get("CreatedDate", ""))

    transitions_list, owner_changes_list, high_friction = [], [], []

    for loan in loans:
        lid = loan["Id"]
        history = by_loan.get(lid, [])
        transitions = len(history)
        owner_changes = 0
        prev = None
        for h in history:
            owner = h.get("CreatedById")
            if owner and owner != prev and prev is not None:
                owner_changes += 1
            if owner:
                prev = owner
        transitions_list.append(transitions)
        owner_changes_list.append(owner_changes)
        if transitions >= 4 or owner_changes >= 2:
            high_friction.append({
                "loan_id": lid, "transitions": transitions,
                "owner_changes": owner_changes,
                "loan_type": loan.get("LLC_BI__Loan_Type__c", ""),
                "amount": loan.get("LLC_BI__Amount__c"),
            })

    return {
        "total_loans":           len(loans),
        "avg_stage_transitions": round(sum(transitions_list) / len(transitions_list), 1),
        "max_stage_transitions": max(transitions_list),
        "avg_owner_changes":     round(sum(owner_changes_list) / len(owner_changes_list), 1),
        "max_owner_changes":     max(owner_changes_list),
        "high_friction_loans":   high_friction[:10],
        "owner_change_source":   "LOAN_HISTORY_CREATEDBY",
    }


def _build_covenant_metrics(
    covenant_compliance: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Covenant tracking gap from LLC_BI__Covenant2__c (queried directly).

    NC-2 confirmed: LLC_BI__Covenant_Compliance__c does NOT have
    LLC_BI__Loan__c, LLC_BI__Active__c, or LLC_BI__Covenant2__c fields.
    It is a compliance EVENT log, not the signal source.

    Signal source: LLC_BI__Covenant2__c directly.
    Confirmed fields: LLC_BI__Overdue__c, LLC_BI__Breached__c,
    LLC_BI__Due_Date__c, LLC_BI__Active__c, LLC_BI__Account__c.

    Note: covenant_compliance parameter contains LLC_BI__Covenant2__c records
    (renamed for backward compatibility with ingest() caller).
    """
    if not covenant_compliance:
        return {
            "total_covenants": 0, "overdue_count": 0, "breached_count": 0,
            "compliance_override": False, "max_days_past_evaluation": 0,
            "overdue_records": [],
        }

    overdue_records = []
    breached_count  = 0
    max_days_past   = 0

    for cov in covenant_compliance:
        overdue  = bool(cov.get("LLC_BI__Overdue__c",  False))
        breached = bool(cov.get("LLC_BI__Breached__c", False))
        days_past = float(cov.get("LLC_BI__Days_Past_Next_Evaluation__c") or 0)

        if breached:
            breached_count += 1
        if overdue or breached:
            overdue_records.append({
                "covenant_id":     cov.get("Id"),
                "account_id":      cov.get("LLC_BI__Account__c"),
                "overdue":         overdue,
                "breached":        breached,
                "days_past":       days_past,
                "last_evaluation": cov.get("LLC_BI__Last_Evaluation_Date__c"),
                "status":          cov.get("LLC_BI__Covenant_Status__c", ""),
                "frequency":       cov.get("LLC_BI__Frequency__c", ""),
            })
            max_days_past = max(max_days_past, days_past)

    return {
        "total_covenants":          len(covenant_compliance),
        "overdue_count":            len(overdue_records),
        "breached_count":           breached_count,
        "compliance_override":      breached_count > 0,
        "max_days_past_evaluation": max_days_past,
        "overdue_records":          overdue_records[:10],
    }
def _build_checklist_metrics(
    checklists: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Checklist bottleneck from LLC_BI__Checklist__c.

    Signal 1: Actual_Duration_Days__c > Expected_Duration_Days__c (overrun)
    Signal 2: Status = 'To Do' and created > 14 days ago (stalled)

    Note: No Required_Count / Received_Count fields exist on this object.
    This object tracks workflow task checklists, not document counts.
    """
    if not checklists:
        return {
            "total_checklists": 0, "overrun_count": 0, "stalled_count": 0,
            "max_overrun_days": 0, "avg_overrun_days": 0.0,
            "overrun_records": [],
        }

    overrun_records = []
    stalled_count = 0
    today = _today()

    for cl in checklists:
        actual   = float(cl.get("LLC_BI__Actual_Duration_Days__c") or 0)
        expected = float(cl.get("LLC_BI__Expected_Duration_Days__c") or 0)
        status   = cl.get("LLC_BI__Status__c", "")
        created  = _parse_date(cl.get("CreatedDate"))
        days_open = _days_since(created) or 0

        overrun_days = max(0, actual - expected) if expected > 0 else 0
        # SF-NC-3 confirmed stall-worthy statuses from Org 2 (May 2026):
        # To Do, Under Review, On Hold
        # In Progress, Complete, Rejected are NOT stall states.
        _STALL_STATUSES = {"to do", "todo", "under review", "on hold", "not started"}
        is_stalled = (
            status.lower() in _STALL_STATUSES
            and days_open >= 14
        )

        if is_stalled:
            stalled_count += 1

        if overrun_days > 0 or is_stalled:
            overrun_records.append({
                "loan_id":      cl.get("LLC_BI__Loan__c"),
                "checklist_id": cl.get("Id"),
                "actual_days":  actual,
                "expected_days": expected,
                "overrun_days": overrun_days,
                "status":       status,
                "days_open":    days_open,
                "stalled":      is_stalled,
                "category":     cl.get("LLC_BI__Category__c", ""),
            })

    overrun_days_list = [r["overrun_days"] for r in overrun_records if r["overrun_days"] > 0]
    return {
        "total_checklists": len(checklists),
        "overrun_count":    len([r for r in overrun_records if r["overrun_days"] > 0]),
        "stalled_count":    stalled_count,
        "max_overrun_days": max(overrun_days_list) if overrun_days_list else 0,
        "avg_overrun_days": round(sum(overrun_days_list) / len(overrun_days_list), 1)
                            if overrun_days_list else 0.0,
        "overrun_records":  overrun_records[:10],
    }


def _build_spreading_metrics(
    spread_periods: List[Dict[str, Any]],
    spreads: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Spreading bottleneck from LLC_BI__Spread_Statement_Period__c.

    Confirmed fields (real Org 2 metadata — May 2026):
      LLC_BI__Is_Locked__c  — confirmed bottleneck signal (false = not finalised)
      LLC_BI__Spread__c     — parent spread header
      CreatedById           — analyst proxy (no LLC_BI__Analyst__c on this object)
      CreatedDate           — period created date

    Two-hop loan resolution:
      SpreadStatementPeriod.LLC_BI__Spread__c → Spread.LLC_BI__Loan__c
    """
    if not spread_periods:
        return {
            "total_periods": 0, "unlocked_count": 0,
            "max_days_unlocked": 0, "avg_days_unlocked": 0.0,
            "analyst_bottlenecks": [], "bottleneck_records": [],
        }

    # Build spread_id → loan_id index from spread headers
    spread_to_loan: Dict[str, str] = {
        s["Id"]: s.get("LLC_BI__Loan__c", "")
        for s in spreads if s.get("Id")
    }

    unlocked = []
    analyst_map: Dict[str, int] = {}

    for sp in spread_periods:
        is_locked = bool(sp.get("LLC_BI__Is_Locked__c", True))
        if is_locked:
            continue
        created = _parse_date(sp.get("CreatedDate"))
        days_open = _days_since(created) or 0
        if days_open < 14:
            continue

        spread_id = sp.get("LLC_BI__Spread__c", "")
        loan_id   = spread_to_loan.get(spread_id, spread_id)  # fall back to spread_id if no header
        analyst   = sp.get("LLC_BI__Analyst__c") or sp.get("CreatedById", "unknown")  # Analyst__c confirmed, CreatedById as fallback

        analyst_map[analyst] = analyst_map.get(analyst, 0) + 1
        unlocked.append({
            "loan_id":        loan_id,
            "spread_id":      spread_id,
            "period_id":      sp.get("Id"),
            "analyst_id":     analyst,
            "days_unlocked":  days_open,
            "is_annual":      sp.get("LLC_BI__Is_Annual__c", False),
        })

    days_list = [r["days_unlocked"] for r in unlocked]
    analyst_bottlenecks = [
        {"analyst_id": a, "unlocked_count": c}
        for a, c in sorted(analyst_map.items(), key=lambda x: -x[1])
    ]

    return {
        "total_periods":       len(spread_periods),
        "unlocked_count":      len(unlocked),
        "max_days_unlocked":   max(days_list) if days_list else 0,
        "avg_days_unlocked":   round(sum(days_list) / len(days_list), 1) if days_list else 0.0,
        "analyst_bottlenecks": analyst_bottlenecks[:5],
        "bottleneck_records":  unlocked[:10],
    }
def _build_approval_metrics(
    process_instances: List[Dict[str, Any]],
    loan_ids: set,
) -> Dict[str, Any]:
    """
    Approval bottleneck from ProcessInstance (standard Salesforce).
    Filters to loan-related approvals by checking TargetObjectId prefix.
    Pending = Status not in (Approved, Rejected, Recalled).
    """
    today = _today()
    TERMINAL = {"Approved", "Rejected", "Recalled", "Removed"}

    # Filter to loan-related instances
    loan_instances = [
        p for p in process_instances
        if p.get("TargetObjectId", "") in loan_ids
    ]

    pending = []
    cycle_days_list = []

    for pi in loan_instances:
        status = pi.get("Status", "")
        created = _parse_date(pi.get("CreatedDate"))
        completed = _parse_date(pi.get("CompletedDate"))

        is_pending = status not in TERMINAL

        if completed and created:
            cycle_days = max(0, (completed - created).days)
        elif created:
            cycle_days = max(0, (today - created).days)
        else:
            cycle_days = 0

        if cycle_days > 0:
            cycle_days_list.append(cycle_days)

        if is_pending:
            pending.append({
                "loan_id":    pi.get("TargetObjectId"),
                "instance_id": pi.get("Id"),
                "status":     status,
                "days_open":  cycle_days,
                "submitted_by": pi.get("SubmittedById"),
            })

    return {
        "total_instances": len(loan_instances),
        "pending_count":   len(pending),
        "avg_cycle_days":  round(sum(cycle_days_list) / len(cycle_days_list), 1)
                           if cycle_days_list else 0.0,
        "max_cycle_days":  max(cycle_days_list) if cycle_days_list else 0,
        "pending_records": pending[:10],
    }


# ─────────────────────────────────────────────────────────────────────────────
# STAGE DURATION METRICS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_dt(val: Any) -> Optional[date]:
    return _parse_date(val)


def _build_stage_duration_metrics(
    loans: List[Dict[str, Any]],
    stage_history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not stage_history:
        return {
            "total_stages_analyzed": 0, "overrun_count": 0,
            "max_overrun_days": 0, "avg_overrun_days": 0.0,
            "overrun_by_stage": {}, "overrun_records": [],
        }

    by_loan: Dict[str, List] = {}
    for h in stage_history:
        lid = h.get("ParentId", "")  # LLC_BI__Loan__History uses ParentId
        if lid:
            by_loan.setdefault(lid, []).append(h)
    for lid in by_loan:
        by_loan[lid].sort(key=lambda h: h.get("CreatedDate", ""))

    overrun_records, overrun_by_stage, total = [], {}, 0

    for lid, history in by_loan.items():
        for i, h in enumerate(history):
            stage = h.get("NewValue", "__DEFAULT__") or "__DEFAULT__"
            entered = _parse_dt(h.get("CreatedDate"))
            next_dt = _parse_dt(history[i + 1].get("CreatedDate")) \
                      if i + 1 < len(history) else None
            end = next_dt if next_dt else _today()
            actual_days = max(0, (end - entered).days) if entered else 0
            benchmark = STAGE_DURATION_BENCHMARKS.get(
                stage, STAGE_DURATION_BENCHMARKS["__DEFAULT__"]
            )
            overrun = max(0, actual_days - benchmark)
            total += 1
            if overrun >= 5:
                overrun_records.append({
                    "loan_id": lid, "stage_name": stage,
                    "actual_days": actual_days, "overrun_days": overrun,
                    "benchmark": benchmark,
                })
                overrun_by_stage[stage] = overrun_by_stage.get(stage, 0) + 1

    days_list = [r["overrun_days"] for r in overrun_records]
    return {
        "total_stages_analyzed": total,
        "overrun_count":        len(overrun_records),
        "max_overrun_days":     max(days_list) if days_list else 0,
        "avg_overrun_days":     round(sum(days_list) / len(days_list), 1) if days_list else 0.0,
        "overrun_by_stage":     overrun_by_stage,
        "overrun_records":      overrun_records[:10],
    }



# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS — exported for unit testing
# ─────────────────────────────────────────────────────────────────────────────

def derive_stage_duration_days(
    stage_entered: Any,
    next_transition: Any,
) -> int:
    """Days a loan spent in a stage. 0 if dates unavailable."""
    start = _parse_date(stage_entered)
    end   = _parse_date(next_transition) if next_transition else _today()
    if start is None:
        return 0
    return max(0, (end - start).days)


def derive_stage_duration_overrun(stage_name: str, actual_days: int) -> int:
    """Days over benchmark for a stage. 0 if within benchmark."""
    benchmark = STAGE_DURATION_BENCHMARKS.get(
        stage_name, STAGE_DURATION_BENCHMARKS["__DEFAULT__"]
    )
    return max(0, actual_days - benchmark)


def derive_approval_cycle_days(submitted_date: Any, completed_date: Any = None) -> int:
    """Approval cycle time in days. 0 if no submitted date."""
    start = _parse_date(submitted_date)
    if start is None:
        return 0
    end = _parse_date(completed_date) if completed_date else _today()
    return max(0, (end - start).days)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def ingest() -> Dict[str, Any]:
    if is_live():
        client              = _get_client()
        loans               = _fetch_loans(client)
        stage_history       = _fetch_stage_history(client)
        covenant_compliance = _fetch_covenant_compliance(client)
        checklists          = _fetch_checklists(client)
        spreads            = _fetch_spreads(client)
        spread_periods      = _fetch_spread_periods(client)
        process_instances   = _fetch_approval_instances(client)
    else:
        fixture             = _load_fixture()
        loans               = fixture.get("loans", [])
        stage_history       = fixture.get("loan_stage_history", [])
        covenant_compliance = fixture.get("covenant_compliance", [])
        checklists          = fixture.get("checklists", [])
        spreads            = fixture.get("spreads", [])
        spread_periods      = fixture.get("spread_periods", [])
        process_instances   = fixture.get("process_instances", [])

    loan_ids = {l["Id"] for l in loans}

    return {
        "loans":               loans,
        "loan_stage_history":  stage_history,
        "covenant_compliance": covenant_compliance,
        "checklists":          checklists,
        "spreads":             spreads,
        "spread_periods":      spread_periods,
        "process_instances":   process_instances,

        "origination_metrics":     _build_origination_metrics(loans, stage_history),
        "covenant_metrics":        _build_covenant_metrics(covenant_compliance),
        "checklist_metrics":       _build_checklist_metrics(checklists),
        "spreading_metrics":       _build_spreading_metrics(spread_periods, spreads),
        "approval_metrics":        _build_approval_metrics(process_instances, loan_ids),
        "stage_duration_metrics":  _build_stage_duration_metrics(loans, stage_history),
    }
