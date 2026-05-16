"""
STRS Benefits Administration Ingestion Module — v1.0
ENG-STRS-1 — Sprint 5.2

Confirmed objects from PSS org metadata (retrieve_09SdN000001yrDlUAI_1.zip):
  IndividualApplication     ✓ Status(Picklist), AppliedDate, ApprovedDate,
                              IsSubmitted, PaymentDate, BenefitAssignmentId
  BenefitAssignment         ✓ Status, ApprovalStatus, NextPayoutDate,
                              PayoutFrequency, StartDateTime, EndDateTime,
                              RecertificationDueDate, RecertificationStatus,
                              EntitlementAmount, EnrolleeId, ProgramEnrollmentId

NOT in this org's metadata — design decisions:
  BenefitDisbursement       → PROXY: BenefitAssignment.NextPayoutDate
                              Detect overdue disbursements via NextPayoutDate < TODAY
                              AND Status not in terminal states.
                              Documented in SF-STRS-3. SME to confirm on trial org.
  BenefitAssignmentAdjustment → NOT built in Sprint 5.2 (Gap 2).
                              SURVIVOR_BENEFIT_DELAY deferred to Sprint 5.3.

IndividualApplication.Status picklist values assumed from PSS documentation:
  Draft, Submitted, In Review, Approved, Rejected, Returned, Withdrawn
  SF-STRS-1 must confirm actual values from trial org and update
  APPLICATION_STALL_STATUSES below before Wave 2 closes.

Thresholds (pre-SME, to be confirmed by SF-STRS-1 / SF-STRS-3):
  APPLICATION_STALL_DAYS:       30  (Ohio Revised Code 3307 guideline)
  ELECTION_DEADLINE_DAYS:       21  (days from BenefitAssignment approval
                                     to required payment election)
  DISBURSEMENT_OVERDUE_DAYS:     0  (any NextPayoutDate < TODAY = overdue)
  WINDOW_DAYS:                  90  (lookback window for all queries)

Corroboration (Fix Pack Sprint 7 — ENG-STRS-CORR-1/2):
  Jira and ServiceNow corroboration now wired in.
  get_jira_strs_correlation() and get_sn_strs_correlation() build
  by_detector dicts consumed by runner.py for evidence attachment.
  Follows exact same pattern as nCino lending_correlation in jira.py/servicenow.py.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import is_live
from .strs_jira_corroboration import (
    get_strs_correlation as get_jira_strs_correlation,
    fetch_strs_jira_issues,
)
from .strs_sn_corroboration import (
    get_strs_correlation as get_sn_strs_correlation,
    fetch_strs_sn_incidents,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "strs_benefits_sample.json"
API_VERSION  = "v66.0"
WINDOW_DAYS  = 90

# ── Thresholds — SF-STRS-1 / SF-STRS-3 to confirm ───────────────────────────

APPLICATION_STALL_DAYS   = 30   # Days from submission before application is stalled
ELECTION_DEADLINE_DAYS   = 21   # Days from BenefitAssignment approval to election required

# IndividualApplication.Status values that indicate a stalled/incomplete application
# SF-STRS-1 to confirm exact picklist values from PSS trial org
# SF-STRS-1 CONFIRMED — cloudfulcrum PSS org — 12 May 2026
# Query: SELECT Status FROM IndividualApplication LIMIT 200
# Values observed: Draft, Submitted, In Review, Returned
APPLICATION_STALL_STATUSES = frozenset([
    "Draft",
    "Returned",
    "In Review",
    "Submitted",    # submitted but not progressing past threshold days
])

# BenefitAssignment.Status values that indicate approval
# SF-STRS-1 to confirm exact picklist values
# SF-STRS-1 CONFIRMED — cloudfulcrum PSS org — 12 May 2026
# Query: SELECT ApprovalStatus, Status FROM BenefitAssignment LIMIT 200
# ApprovalStatus observed: Approved | Status observed: Active
BENEFIT_APPROVED_STATUSES = frozenset([
    "Approved",
])

# BenefitAssignment.Status values where disbursement should be running
DISBURSEMENT_ACTIVE_STATUSES = frozenset([
    "Active",
    "Approved",
])

# Statuses that indicate election is pending (approved but payment not set up)
# SF-STRS-1 to confirm exact values from trial org
ELECTION_PENDING_STATUSES = frozenset([
    "Under Review",
    "Pending",
    "Pending Election",
    "Approved",  # Approved but NextPayoutDate/PayoutFrequency not set = pending election
])


# ── Custom exception ──────────────────────────────────────────────────────────

class StrsIngestError(Exception):
    pass


# ── Salesforce client (reuses SF credentials — same org as nCino) ─────────────

def _get_client():
    import requests
    from token_generation.strs.token_strs import get_token

    try:
        access_token, instance_url = get_token()
    except Exception as e:
        raise StrsIngestError(f"STRS token load failed: {e}")

    logger.info("STRS JWT token: OK — loaded from strs_token.json")

    class _SFClient:
        def __init__(self, base_url: str, token: str):
            self.base_url = base_url
            self.token    = token

        def query(self, soql: str) -> List[Dict[str, Any]]:
            url     = f"{self.base_url}/services/data/{API_VERSION}/query"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept":        "application/json",
            }
            params  = {"q": soql}
            resp    = requests.get(url, headers=headers, params=params, timeout=30)
            if not resp.ok:
                raise StrsIngestError(
                    f"SOQL failed ({resp.status_code}): {resp.text}"
                )
            return resp.json().get("records", [])

    return _SFClient(instance_url, access_token)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    try:
        if isinstance(val, date):
            return val
        s = str(val)[:10]
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _days_since(d: Optional[date]) -> Optional[int]:
    if d is None:
        return None
    return (_today() - d).days


def _days_until(d: Optional[date]) -> Optional[int]:
    if d is None:
        return None
    return (d - _today()).days


# ── Fetch functions (live mode) ───────────────────────────────────────────────

def _fetch_applications(client) -> List[Dict[str, Any]]:
    """
    IndividualApplication — service retirement applications.

    Confirmed fields (PSS metadata):
      Status, AppliedDate, ApprovedDate, IsSubmitted, PaymentDate,
      BenefitAssignmentId, ApplicationType, InternalStatus

    SF-STRS-1: confirm Status picklist values and stall threshold.
    """
    rows = client.query(f"""
        SELECT Id, Name, Status, AppliedDate, ApprovedDate,
               IsSubmitted, PaymentDate, BenefitAssignmentId,
               ApplicationType, InternalStatus, Description
        FROM IndividualApplication
        WHERE AppliedDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("applications=%d", len(rows))
    return rows


def _fetch_benefit_assignments(client) -> List[Dict[str, Any]]:
    """
    BenefitAssignment — approved benefit awards.

    SF-STRS-1 CONFIRMED — cloudfulcrum PSS org — 12 May 2026:
      ApprovalStatus observed: Approved
      Status observed: Active
    Used for BENEFIT_ELECTION_DEADLINE detection only.
    DISBURSEMENT_OVERDUE now uses _fetch_disbursements() directly.
    """
    # AssignmentDate,
    # WHERE AssignmentDate = LAST_N_DAYS:{WINDOW_DAYS}
    # RecertificationStatus,

    rows = client.query(f"""
        SELECT Id, Name, Status, ApprovalStatus, AssignmentDate,
               StartDateTime, EndDateTime, PayoutFrequency,
               EntitlementAmount, EnrolleeId, ProgramEnrollmentId,
               RecertificationDueDate, RecertificationStatus,
               TotalApprovedAmount, TotalPaidAmount, BenefitId
        FROM BenefitAssignment
        WHERE AssignmentDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("benefit_assignments=%d", len(rows))
    return rows


def _fetch_disbursements(client) -> List[Dict[str, Any]]:
    """
    BenefitDisbursement — individual benefit payments.

    SF-STRS-3 CONFIRMED — cloudfulcrum PSS org — 12 May 2026:
      Query: SELECT Id, ApprovalStatus, DisbursementStatus, PaymentStatus,
             StartDate, EndDate, ActualCompletionDate, BenefitAssignmentId,
             AdjustmentAmount, EntitlementAmount, PayoutAmount
      FROM BenefitDisbursement — Total rows: 200

    Confirmed fields:
      ApprovalStatus:      Approved
      DisbursementStatus:  Completed
      PaymentStatus:       Paid
      EndDate:             disbursement period end — used as scheduled payment date
      ActualCompletionDate: when payment was made — null if not yet paid
      PayoutAmount:        actual payment amount

    Overdue logic: EndDate < TODAY AND PaymentStatus != 'Paid'
    In trial org all records are Paid — detector correctly does not fire.
    In a real STRS org with pending payments it will fire.

    Note: ScheduledDate does NOT exist — EndDate is the payment due date.
    Note: Amount field does NOT exist — PayoutAmount is the payment amount.
    """
    rows = client.query(f"""
        SELECT Id, ApprovalStatus, DisbursementStatus, PaymentStatus,
               StartDate, EndDate, ActualCompletionDate,
               BenefitAssignmentId, AdjustmentAmount,
               EntitlementAmount, PayoutAmount
        FROM BenefitDisbursement
        WHERE StartDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("disbursements=%d", len(rows))
    return rows


def _fetch_disability_cases(client) -> List[Dict[str, Any]]:
    """
    Case — disability benefit applications.

    STRS disability review goes through Case management.
    RecordType distinguishes disability from other case types.

    SF-STRS-2: confirm RecordType API name for disability cases.
    Assumption: Type = 'Disability' or RecordType.Name contains 'Disability'
    Using Type field as proxy — SF-STRS-2 to confirm exact value.
    """
    rows = client.query(f"""
        SELECT Id, CaseNumber, Status, Type, Origin,
               CreatedDate, ClosedDate, Description,
               AccountId, ContactId
        FROM Case
        WHERE CreatedDate = LAST_N_DAYS:{WINDOW_DAYS}
        AND Type IN ('Disability', 'Disability Benefit', 'Disability Review')
        LIMIT 5000
    """)
    logger.info("disability_cases=%d", len(rows))
    return rows


# ── Metric builders ───────────────────────────────────────────────────────────

def _build_application_metrics(
    applications: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    APPLICATION_STALL detector input.
    Stalled = Status in APPLICATION_STALL_STATUSES AND days since AppliedDate >= threshold.
    """
    today = _today()
    stalled = []

    for app in applications:
        status = app.get("Status", "")
        applied = _parse_date(app.get("AppliedDate"))
        days = _days_since(applied) if applied else None

        if (
            status in APPLICATION_STALL_STATUSES
            and days is not None
            and days >= APPLICATION_STALL_DAYS
        ):
            stalled.append({
                "id":     app.get("Id"),
                "status": status,
                "days":   days,
            })

    stalled_days = [s["days"] for s in stalled]

    return {
        "total_applications":   len(applications),
        "stalled_count":        len(stalled),
        "max_days_stalled":     max(stalled_days) if stalled_days else 0,
        "avg_days_stalled":     round(sum(stalled_days) / len(stalled_days), 1) if stalled_days else 0.0,
        "stall_threshold_days": APPLICATION_STALL_DAYS,
        "primary_object":       "IndividualApplication",
        "sme_note":             "SF-STRS-1 to confirm Status picklist values and stall threshold",
    }


def _build_election_metrics(
    benefit_assignments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    BENEFIT_ELECTION_DEADLINE detector input.
    Overdue = ApprovalStatus indicates approved AND days since AssignmentDate >= ELECTION_DEADLINE_DAYS
    AND Status does NOT indicate active disbursement (election not completed).

    SF-STRS-1: confirm which Status/ApprovalStatus combination = approved-but-unenrolled.
    """
    overdue = []

    for ba in benefit_assignments:
        approval_status = ba.get("ApprovalStatus", "")
        status          = ba.get("Status", "")
        assigned        = _parse_date(ba.get("AssignmentDate"))
        days_since      = _days_since(assigned) if assigned else None

        # Election overdue: approved AND no NextPayoutDate set AND past deadline
        # NextPayoutDate null = payment method not elected yet
        next_payout = ba.get("NextPayoutDate")
        payout_freq = ba.get("PayoutFrequency")
        is_approved = (
            approval_status in BENEFIT_APPROVED_STATUSES
            or status in BENEFIT_APPROVED_STATUSES
        )
        election_not_done = (not next_payout and not payout_freq)

        if (
            is_approved
            and election_not_done
            and days_since is not None
            and days_since >= ELECTION_DEADLINE_DAYS
        ):
            overdue.append({
                "id":          ba.get("Id"),
                "status":      status,
                "approval":    approval_status,
                "days":        days_since,
            })

    overdue_days = [o["days"] for o in overdue]

    return {
        "total_assignments":        len(benefit_assignments),
        "overdue_election_count":   len(overdue),
        "max_days_overdue":         max(overdue_days) if overdue_days else 0,
        "election_deadline_days":   ELECTION_DEADLINE_DAYS,
        "default_plan_risk":        len(overdue) > 0,
        "primary_object":           "BenefitAssignment",
        "sme_note":                 "SF-STRS-1 to confirm ApprovalStatus vs Status for election detection",
    }


def _build_disbursement_metrics(
    disbursements: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    DISBURSEMENT_OVERDUE detector input.

    SF-STRS-3 CONFIRMED — BenefitDisbursement exists in cloudfulcrum PSS org.
    Proxy removed. Now queries BenefitDisbursement directly.

    Overdue logic: EndDate < TODAY AND PaymentStatus != 'Paid'
    Fields confirmed: EndDate (payment due date), PaymentStatus, PayoutAmount.

    Note: In PSS trial org all 200 records are PaymentStatus=Paid and
    DisbursementStatus=Completed — detector correctly does not fire against
    clean trial data. It will fire in a real STRS org with pending payments.
    """
    today = _today()

    PAID_STATUSES = frozenset(["Paid", "Completed"])
    overdue = []

    for d in disbursements:
        payment_status  = d.get("PaymentStatus", "")
        end_date        = _parse_date(d.get("EndDate"))

        if (
            payment_status not in PAID_STATUSES
            and end_date is not None
            and end_date < today
        ):
            days_overdue = (today - end_date).days
            overdue.append({
                "id":           d.get("Id"),
                "end_date":     str(end_date),
                "days_overdue": days_overdue,
                "payment_status": payment_status,
                "disbursement_status": d.get("DisbursementStatus", ""),
                "payout_amount": d.get("PayoutAmount", 0),
            })

    overdue_days = [o["days_overdue"] for o in overdue]

    return {
        "total_disbursements":  len(disbursements),
        "overdue_count":        len(overdue),
        "max_days_overdue":     max(overdue_days) if overdue_days else 0,
        "avg_days_overdue":     round(sum(overdue_days) / len(overdue_days), 1) if overdue_days else 0.0,
        "primary_object":       "BenefitDisbursement",
        "date_field_used":      "EndDate",
        "compliance_override":  True,   # Any overdue disbursement = regulatory obligation
        "sme_confirmed":        "SF-STRS-3 — 12 May 2026",
    }


def _build_disability_metrics(
    disability_cases: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    DISABILITY_REVIEW_BOTTLENECK detector input.
    Pending = disability Case with Status not Closed AND days since CreatedDate >= threshold.

    SF-STRS-2: confirm Case RecordType for disability and review stage field.
    compliance_override_threshold: 30 days (proxy for member stopped work).
    """
    today = _today()
    REVIEW_THRESHOLD_DAYS = 30  # SF-STRS-2 to confirm (STRS publishes up to 6 months max)

    pending = []

    for case in disability_cases:
        status      = case.get("Status", "")
        created     = _parse_date(case.get("CreatedDate"))
        days        = _days_since(created) if created else None
        is_closed   = "Closed" in status or "Resolved" in status

        if not is_closed and days is not None and days >= REVIEW_THRESHOLD_DAYS:
            pending.append({
                "id":   case.get("Id"),
                "days": days,
                "status": status,
            })

    pending_days = [p["days"] for p in pending]
    max_days = max(pending_days) if pending_days else 0

    return {
        "total_disability_cases":   len(disability_cases),
        "pending_review_count":     len(pending),
        "max_days_pending":         max_days,
        "avg_days_pending":         round(sum(pending_days) / len(pending_days), 1) if pending_days else 0.0,
        "review_threshold_days":    REVIEW_THRESHOLD_DAYS,
        # compliance_override when member has stopped work — proxy: max_days >= 30
        "member_stopped_work":      max_days >= REVIEW_THRESHOLD_DAYS and len(pending) > 0,
        "primary_object":           "Case",
        "sme_note":                 "SF-STRS-2 to confirm Case RecordType for disability and stage field",
    }


def _build_strs_metrics(
    applications:       List[Dict[str, Any]],
    benefit_assignments: List[Dict[str, Any]],
    disbursements:       List[Dict[str, Any]],
    disability_cases:   List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compose all 4 metric dicts from raw fetched data."""
    return {
        "application_metrics":  _build_application_metrics(applications),
        "election_metrics":     _build_election_metrics(benefit_assignments),
        "disbursement_metrics": _build_disbursement_metrics(disbursements),
        "disability_metrics":   _build_disability_metrics(disability_cases),
    }


# ── Safe fetch wrapper ────────────────────────────────────────────────────────

def _safe_fetch(fn, client, label: str, default=None):
    """Per-query fault isolation. One SOQL failure never kills all detectors."""
    try:
        return fn(client)
    except Exception as e:
        logger.warning("STRS %s fetch failed (non-blocking): %s", label, e)
        return default if default is not None else []


# ── Main ingest() ─────────────────────────────────────────────────────────────

def ingest() -> Dict[str, Any]:
    """
    Entry point called by runner.py for pack=strs_benefits.
    Returns a dict with all metric dicts needed by the 4 detectors.
    """
    if is_live():
        client = _get_client()

        applications        = _safe_fetch(_fetch_applications,       client, "applications")
        benefit_assignments = _safe_fetch(_fetch_benefit_assignments, client, "benefit_assignments")
        disbursements       = _safe_fetch(_fetch_disbursements,       client, "disbursements")
        disability_cases    = _safe_fetch(_fetch_disability_cases,    client, "disability_cases")

        metrics = _build_strs_metrics(
            applications, benefit_assignments, disbursements, disability_cases
        )

        logger.info(
            "STRS ingestion: OK — applications=%d assignments=%d disbursements=%d disability_cases=%d",
            len(applications), len(benefit_assignments), len(disbursements), len(disability_cases),
        )

        # ── Jira corroboration ────────────────────────────────────────────
        # Fetch STRS-relevant Jira issues and build by_detector correlation.
        # Non-blocking — Jira failure does not abort STRS ingestion.
        jira_strs_correlation = {"by_detector": {}, "total_matched": 0, "strs_issues": []}
        try:
            from .jira import _get_client as _get_jira_client
            jira_client = _get_jira_client()
            jira_issues = fetch_strs_jira_issues(jira_client)
            jira_strs_correlation = get_jira_strs_correlation(jira_issues)
            logger.info(
                "Jira STRS correlation: %d issues matched",
                jira_strs_correlation["total_matched"],
            )
        except Exception as e:
            logger.warning("Jira STRS corroboration failed (non-blocking): %s", e)

        # ── ServiceNow corroboration ──────────────────────────────────────
        sn_strs_correlation = {"by_detector": {}, "total_matched": 0, "strs_incidents": []}
        try:
            from .servicenow import _get_client as _get_sn_client
            sn_client = _get_sn_client()
            sn_incidents = fetch_strs_sn_incidents(sn_client)
            sn_strs_correlation = get_sn_strs_correlation(sn_incidents)
            logger.info(
                "ServiceNow STRS correlation: %d incidents matched",
                sn_strs_correlation["total_matched"],
            )
        except Exception as e:
            logger.warning("ServiceNow STRS corroboration failed (non-blocking): %s", e)

        metrics["jira_strs_correlation"] = jira_strs_correlation
        metrics["sn_strs_correlation"]   = sn_strs_correlation

        return metrics

    # ── Offline fixture path ──────────────────────────────────────────────────
    if not FIXTURE_PATH.exists():
        raise StrsIngestError(
            f"Offline fixture not found: {FIXTURE_PATH}. "
            "Create discovery/ingest/fixtures/strs_benefits_sample.json or set INGEST_MODE=live."
        )

    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        fixture = json.load(f)

    applications        = fixture.get("applications", [])
    benefit_assignments = fixture.get("benefit_assignments", [])
    disbursements       = fixture.get("disbursements", [])
    disability_cases    = fixture.get("disability_cases", [])

    logger.info(
        "STRS ingestion: fixture — applications=%d assignments=%d disbursements=%d disability_cases=%d",
        len(applications), len(benefit_assignments), len(disbursements), len(disability_cases),
    )

    metrics = _build_strs_metrics(
        applications, benefit_assignments, disbursements, disability_cases
    )

    # ── Offline corroboration — load from seed fixtures ───────────────────────
    import json as _json
    from pathlib import Path as _Path

    _JIRA_SEED   = _Path(__file__).parent / "fixtures" / "strs_jira_seed.json"
    _SN_SEED     = _Path(__file__).parent / "fixtures" / "strs_sn_seed.json"

    jira_strs_correlation = {"by_detector": {}, "total_matched": 0, "strs_issues": []}
    if _JIRA_SEED.exists():
        try:
            _jira_seed = _json.loads(_JIRA_SEED.read_text())
            jira_issues = _jira_seed.get("issues", [])
            jira_strs_correlation = get_jira_strs_correlation(jira_issues)
            logger.info(
                "Jira STRS correlation (offline seed): %d issues matched",
                jira_strs_correlation["total_matched"],
            )
        except Exception as e:
            logger.warning("Jira STRS seed load failed: %s", e)

    sn_strs_correlation = {"by_detector": {}, "total_matched": 0, "strs_incidents": []}
    if _SN_SEED.exists():
        try:
            _sn_seed = _json.loads(_SN_SEED.read_text())
            sn_incidents = _sn_seed.get("incidents", [])
            sn_strs_correlation = get_sn_strs_correlation(sn_incidents)
            logger.info(
                "ServiceNow STRS correlation (offline seed): %d incidents matched",
                sn_strs_correlation["total_matched"],
            )
        except Exception as e:
            logger.warning("ServiceNow STRS seed load failed: %s", e)

    metrics["jira_strs_correlation"] = jira_strs_correlation
    metrics["sn_strs_correlation"]   = sn_strs_correlation

    return metrics
