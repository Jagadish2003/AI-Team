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

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "strs_benefits_sample.json"
API_VERSION  = "v59.0"
WINDOW_DAYS  = 90

# ── Thresholds — SF-STRS-1 / SF-STRS-3 to confirm ───────────────────────────

APPLICATION_STALL_DAYS   = 30   # Days from submission before application is stalled
ELECTION_DEADLINE_DAYS   = 21   # Days from BenefitAssignment approval to election required

# IndividualApplication.Status values that indicate a stalled/incomplete application
# SF-STRS-1 to confirm exact picklist values from PSS trial org
APPLICATION_STALL_STATUSES = frozenset([
    "Draft",
    "Returned",
    "In Review",
    "Submitted",    # submitted but not progressing
])

# BenefitAssignment.Status values that indicate approval
# SF-STRS-1 to confirm exact picklist values
BENEFIT_APPROVED_STATUSES = frozenset([
    "Approved",
    "Active",
])

# BenefitAssignment.Status values that indicate disbursement is active
# (i.e. NextPayoutDate should be in the future — if in the past, overdue)
DISBURSEMENT_ACTIVE_STATUSES = frozenset([
    "Active",
    "Approved",
])


# ── Custom exception ──────────────────────────────────────────────────────────

class StrsIngestError(Exception):
    pass


# ── Salesforce client (reuses SF credentials — same org as nCino) ─────────────

def _get_client():
    """
    Returns a minimal Salesforce REST client using SF_INSTANCE_URL + SF_ACCESS_TOKEN.
    STRS pack connects to a PSS-enabled Salesforce org — same credential pattern as nCino.
    """
    sf_url   = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
    sf_token = os.getenv("SF_ACCESS_TOKEN", "")

    if not sf_url or not sf_token:
        raise StrsIngestError(
            "SF_INSTANCE_URL and SF_ACCESS_TOKEN required for STRS live mode. "
            "Set INGEST_MODE=offline to use fixture data."
        )

    class _SFClient:
        def __init__(self, base_url: str, token: str):
            self.base_url = base_url
            self.token    = token

        def query(self, soql: str) -> List[Dict[str, Any]]:
            import requests
            url     = f"{self.base_url}/services/data/{API_VERSION}/query"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept":        "application/json",
            }
            params  = {"q": soql}
            resp    = requests.get(url, headers=headers, params=params, timeout=30)
            if not resp.ok:
                raise StrsIngestError(
                    f"SOQL failed ({resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
            return data.get("records", [])

    return _SFClient(sf_url, sf_token)


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

    Confirmed fields (PSS metadata):
      Status, ApprovalStatus, AssignmentDate, StartDateTime, EndDateTime,
      NextPayoutDate, PayoutFrequency, EntitlementAmount, EnrolleeId,
      ProgramEnrollmentId, RecertificationDueDate, RecertificationStatus,
      TotalApprovedAmount, TotalPaidAmount, BenefitId

    Used for:
      - BENEFIT_ELECTION_DEADLINE: approved but election not submitted
      - DISBURSEMENT_OVERDUE: NextPayoutDate < TODAY (proxy for BenefitDisbursement)

    SF-STRS-1: confirm ApprovalStatus vs Status for election detection.
    SF-STRS-3: confirm NextPayoutDate as disbursement proxy.
    """
    rows = client.query(f"""
        SELECT Id, Name, Status, ApprovalStatus, AssignmentDate,
               StartDateTime, EndDateTime, NextPayoutDate, PayoutFrequency,
               EntitlementAmount, EnrolleeId, ProgramEnrollmentId,
               RecertificationDueDate, RecertificationStatus,
               TotalApprovedAmount, TotalPaidAmount, BenefitId
        FROM BenefitAssignment
        WHERE AssignmentDate = LAST_N_DAYS:{WINDOW_DAYS}
        LIMIT 5000
    """)
    logger.info("benefit_assignments=%d", len(rows))
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

        # Approved but not yet active (election pending)
        is_approved     = approval_status in BENEFIT_APPROVED_STATUSES or status in BENEFIT_APPROVED_STATUSES
        is_not_active   = status not in DISBURSEMENT_ACTIVE_STATUSES

        if (
            is_approved
            and is_not_active
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
    benefit_assignments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    DISBURSEMENT_OVERDUE detector input.

    PROXY DESIGN: BenefitDisbursement not in this org's PSS metadata.
    Using BenefitAssignment.NextPayoutDate as disbursement proxy:
      - If NextPayoutDate < TODAY AND Status is active = disbursement overdue
      - This is the same pattern used for LLC_BI__Spread__c in nCino

    SF-STRS-3: confirm this proxy or provide BenefitDisbursement object
    if available in the PSS trial org.
    """
    today = _today()
    overdue = []

    for ba in benefit_assignments:
        status       = ba.get("Status", "")
        next_payout  = _parse_date(ba.get("NextPayoutDate"))

        if (
            status in DISBURSEMENT_ACTIVE_STATUSES
            and next_payout is not None
            and next_payout < today
        ):
            days_overdue = (today - next_payout).days
            overdue.append({
                "id":           ba.get("Id"),
                "next_payout":  str(next_payout),
                "days_overdue": days_overdue,
                "status":       status,
            })

    overdue_days = [o["days_overdue"] for o in overdue]

    return {
        "total_assignments":    len(benefit_assignments),
        "overdue_count":        len(overdue),
        "max_days_overdue":     max(overdue_days) if overdue_days else 0,
        "avg_days_overdue":     round(sum(overdue_days) / len(overdue_days), 1) if overdue_days else 0.0,
        "proxy_field":          "BenefitAssignment.NextPayoutDate",
        "proxy_note":           (
            "BenefitDisbursement not in PSS metadata. "
            "NextPayoutDate used as proxy. SF-STRS-3 to confirm."
        ),
        "primary_object":       "BenefitAssignment",
        "compliance_override":  True,   # Any overdue disbursement = regulatory obligation
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
    disability_cases:   List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compose all 4 metric dicts from raw fetched data."""
    return {
        "application_metrics":  _build_application_metrics(applications),
        "election_metrics":     _build_election_metrics(benefit_assignments),
        "disbursement_metrics": _build_disbursement_metrics(benefit_assignments),
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
        disability_cases    = _safe_fetch(_fetch_disability_cases,   client, "disability_cases")

        metrics = _build_strs_metrics(
            applications, benefit_assignments, disability_cases
        )

        logger.info(
            "STRS ingestion: OK — applications=%d assignments=%d disability_cases=%d",
            len(applications), len(benefit_assignments), len(disability_cases),
        )
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
    disability_cases    = fixture.get("disability_cases", [])

    logger.info(
        "STRS ingestion: fixture — applications=%d assignments=%d disability_cases=%d",
        len(applications), len(benefit_assignments), len(disability_cases),
    )

    return _build_strs_metrics(
        applications, benefit_assignments, disability_cases
    )
