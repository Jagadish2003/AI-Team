"""
SF-2.8 — Demo Seeder

Seeds cross-system reference data into Salesforce, ServiceNow, and Jira
to make the D7 (CROSS_SYSTEM_ECHO) signal fire in live mode demos.

Cross-system consistency contract:
    SF Cases CS-1001..CS-1075 reference real SN incident IDs (INC-10042..INC-10388)
    SF Cases CS-1001..CS-1075 reference real Jira issue keys (JIRA-4421..JIRA-4788)
    SN incidents reference the SF Case IDs (CS-1001..CS-1075)
    Jira issues carry label 'Salesforce' and reference CS- IDs in summary

Usage:
    python -m backend.discovery.seed.demo_seeder --systems all
    python -m backend.discovery.seed.demo_seeder --systems sf sn
    python -m backend.discovery.seed.demo_seeder --rollback

AgentIQ is READ-ONLY for ingestion. The seeder is a WRITE tool used ONLY
to prepare demo environments. It is never called by the runner or detectors.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SEED_STATE_PATH = Path(__file__).parent / "seed_state.json"


# ─────────────────────────────────────────────────────────────────────────────
# Seed state — tracks all created IDs for rollback
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SeedState:
    sf_case_ids:       List[str] = field(default_factory=list)
    sn_incident_sys_ids: List[str] = field(default_factory=list)
    jira_issue_keys:   List[str] = field(default_factory=list)
    seeded_at:         str = ""

    def save(self):
        SEED_STATE_PATH.write_text(json.dumps({
            "sf_case_ids":          self.sf_case_ids,
            "sn_incident_sys_ids":  self.sn_incident_sys_ids,
            "jira_issue_keys":      self.jira_issue_keys,
            "seeded_at":            self.seeded_at,
        }, indent=2), encoding="utf-8")
        logger.info(f"Seed state saved to {SEED_STATE_PATH}")

    @classmethod
    def load(cls) -> "SeedState":
        if not SEED_STATE_PATH.exists():
            return cls()
        data = json.loads(SEED_STATE_PATH.read_text(encoding="utf-8"))
        return cls(**data)


# ─────────────────────────────────────────────────────────────────────────────
# Cross-system reference data
# Must be internally consistent: SF case IDs ↔ SN incident IDs ↔ Jira keys
# ─────────────────────────────────────────────────────────────────────────────

# 10 cross-system clusters — each cluster is one customer problem tracked
# across all 3 systems. Expanding this to 75/80/62 in SF-3.1 live seeding.
CROSS_SYSTEM_CLUSTERS = [
    {"cs": "CS-1001", "inc": "INC-10042", "jira": "JIRA-4421",
     "subject": "Login failure — unable to access portal",
     "sn_short": "Follow-up on Salesforce case CS-1001 — portal login issue",
     "jira_summary": "Fix CS-1001 portal authentication integration bug"},
    {"cs": "CS-1007", "inc": "INC-10091", "jira": "JIRA-4489",
     "subject": "Payment processing error — see INC-10091",
     "sn_short": "Incident raised for Salesforce case CS-1007 payment error",
     "jira_summary": "Resolve CS-1007 payment gateway timeout (Jira-4489)"},
    {"cs": "CS-1015", "inc": "INC-10103", "jira": "JIRA-4501",
     "subject": "Data sync failure between CRM and ERP — JIRA-4501",
     "sn_short": "CS-1015 data sync failure — IT investigation INC-10103",
     "jira_summary": "CS-1015: ERP data sync failure — duplicate of INC-10103"},
    {"cs": "CS-1023", "inc": "INC-10156", "jira": "JIRA-4512",
     "subject": "Email notification not sending — linked INC-10156",
     "sn_short": "Email system outage affecting CS-1023 — INC-10156",
     "jira_summary": "CS-1023 email integration fix — see also INC-10156"},
    {"cs": "CS-1031", "inc": "INC-10211", "jira": "JIRA-4612",
     "subject": "API rate limiting causing case delays — JIRA-4612",
     "sn_short": "API throttling — escalation from CS-1031",
     "jira_summary": "Rate limit increase request — CS-1031 ongoing impact"},
    {"cs": "CS-1042", "inc": "INC-10244", "jira": "JIRA-4634",
     "subject": "Dashboard report incorrect — cross-ref INC-10244",
     "sn_short": "Reporting system error linked to CS-1042",
     "jira_summary": "Fix dashboard discrepancy CS-1042 (JIRA-4634)"},
    {"cs": "CS-1055", "inc": "INC-10299", "jira": "JIRA-4701",
     "subject": "Mobile app crash on login — see JIRA-4701",
     "sn_short": "Mobile app outage — CS-1055 escalated to INC-10299",
     "jira_summary": "CS-1055 mobile crash hotfix — INC-10299 root cause"},
    {"cs": "CS-1061", "inc": "INC-10312", "jira": "JIRA-4745",
     "subject": "Bulk data import failing — INC-10312 raised",
     "sn_short": "Bulk import error from CS-1061 — INC-10312",
     "jira_summary": "CS-1061 bulk import pipeline fix — JIRA-4745"},
    {"cs": "CS-1068", "inc": "INC-10355", "jira": "JIRA-4762",
     "subject": "SSO integration broken after patch — JIRA-4762",
     "sn_short": "SSO failure post-patch CS-1068 — INC-10355",
     "jira_summary": "SSO patch rollback — CS-1068 regression JIRA-4762"},
    {"cs": "CS-1075", "inc": "INC-10388", "jira": "JIRA-4788",
     "subject": "Customer account merge failing — see INC-10388",
     "sn_short": "Account merge failure CS-1075 — IT ticket INC-10388",
     "jira_summary": "CS-1075 account merge bug — duplicate of INC-10388"},
]


# ─────────────────────────────────────────────────────────────────────────────
# Salesforce seeder
# ─────────────────────────────────────────────────────────────────────────────

def seed_salesforce(state: SeedState, dry_run: bool = False) -> int:
    """
    Create Cases in Salesforce with cross-system references in Subject/Description.
    Requires SF_INSTANCE_URL + SF_ACCESS_TOKEN env vars.

    Returns: count of Cases created.
    """
    instance_url = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
    access_token = os.getenv("SF_ACCESS_TOKEN", "")

    if not instance_url or not access_token:
        logger.error(
            "SF_INSTANCE_URL and SF_ACCESS_TOKEN required for Salesforce seeding. "
            "Set these env vars before running the seeder."
        )
        return 0

    if dry_run:
        logger.info(f"[DRY RUN] Would create {len(CROSS_SYSTEM_CLUSTERS)} Salesforce Cases")
        for c in CROSS_SYSTEM_CLUSTERS:
            logger.info(f"  [DRY RUN] SF Case: {c['cs']} — {c['subject'][:60]}")
        return len(CROSS_SYSTEM_CLUSTERS)

    try:
        import requests
    except ImportError:
        logger.error("requests library required: pip install requests")
        return 0

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    })

    created = 0
    for cluster in CROSS_SYSTEM_CLUSTERS:
        payload = {
            "Subject": cluster["subject"],
            "Description": (
                f"This case is tracked across multiple systems.\n"
                f"ServiceNow incident: {cluster['inc']}\n"
                f"Jira issue: {cluster['jira']}\n"
                f"Salesforce case reference: {cluster['cs']}"
            ),
            "Status": "New",
            "Origin": "Web",
        }
        try:
            resp = session.post(
                f"{instance_url}/services/data/v59.0/sobjects/Case/",
                json=payload, timeout=15
            )
            resp.raise_for_status()
            sf_id = resp.json().get("id", "")
            state.sf_case_ids.append(sf_id)
            logger.info(f"  Created SF Case {cluster['cs']} → {sf_id}")
            created += 1
            time.sleep(0.1)  # Respect API limits
        except Exception as e:
            logger.error(f"  Failed to create SF Case {cluster['cs']}: {e}")

    logger.info(f"Salesforce: {created}/{len(CROSS_SYSTEM_CLUSTERS)} Cases created")
    return created


# ─────────────────────────────────────────────────────────────────────────────
# ServiceNow seeder
# ─────────────────────────────────────────────────────────────────────────────

def seed_servicenow(state: SeedState, dry_run: bool = False) -> int:
    """
    Create incidents in ServiceNow referencing Salesforce CS- Case IDs.
    Requires SERVICENOW_URL + (SERVICENOW_TOKEN or SERVICENOW_USER+SERVICENOW_PASS).

    Returns: count of incidents created.
    """
    sn_url = os.getenv("SERVICENOW_URL", "").rstrip("/")
    token  = os.getenv("SERVICENOW_TOKEN", "")
    user   = os.getenv("SERVICENOW_USER", "")
    passwd = os.getenv("SERVICENOW_PASS", "")

    if not sn_url:
        logger.error(
            "SERVICENOW_URL required for ServiceNow seeding."
        )
        return 0

    if dry_run:
        logger.info(f"[DRY RUN] Would create {len(CROSS_SYSTEM_CLUSTERS)} ServiceNow incidents")
        for c in CROSS_SYSTEM_CLUSTERS:
            logger.info(f"  [DRY RUN] SN Incident: {c['inc']} refs {c['cs']}")
        return len(CROSS_SYSTEM_CLUSTERS)

    try:
        import requests
    except ImportError:
        logger.error("requests library required: pip install requests")
        return 0

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    elif user and passwd:
        session.auth = (user, passwd)
    else:
        logger.error("SERVICENOW_TOKEN or SERVICENOW_USER+SERVICENOW_PASS required")
        return 0

    created = 0
    for cluster in CROSS_SYSTEM_CLUSTERS:
        payload = {
            "short_description": cluster["sn_short"],
            "description": (
                f"This incident was created as a follow-up to Salesforce case "
                f"{cluster['cs']}.\n\n"
                f"Cross-reference: {cluster['cs']} in Salesforce CRM.\n"
                f"Jira tracking: {cluster['jira']}"
            ),
            "work_notes": f"Linked to Salesforce case {cluster['cs']} and Jira {cluster['jira']}",
            "category": "software",
            "urgency": "3",
            "impact": "3",
        }
        try:
            resp = session.post(
                f"{sn_url}/api/now/table/incident",
                json=payload, timeout=15
            )
            resp.raise_for_status()
            sys_id = resp.json().get("result", {}).get("sys_id", "")
            state.sn_incident_sys_ids.append(sys_id)
            logger.info(f"  Created SN incident {cluster['inc']} (sys_id={sys_id[:8]}...) refs {cluster['cs']}")
            created += 1
            time.sleep(0.1)
        except Exception as e:
            logger.error(f"  Failed to create SN incident for {cluster['cs']}: {e}")

    logger.info(f"ServiceNow: {created}/{len(CROSS_SYSTEM_CLUSTERS)} incidents created")
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Jira seeder
# ─────────────────────────────────────────────────────────────────────────────

def seed_jira(state: SeedState, dry_run: bool = False) -> int:
    """
    Create issues in Jira with 'Salesforce' label and CS- reference in summary.
    Requires JIRA_URL + JIRA_TOKEN (+ JIRA_USER for Cloud).

    Returns: count of issues created.
    """
    jira_url     = os.getenv("JIRA_URL", "").rstrip("/")
    token        = os.getenv("JIRA_TOKEN", "")
    user         = os.getenv("JIRA_USER", "")
    project_key  = os.getenv("JIRA_PROJECT_KEY", "CRM")

    if not jira_url or not token:
        logger.error("JIRA_URL and JIRA_TOKEN required for Jira seeding.")
        return 0

    if dry_run:
        logger.info(f"[DRY RUN] Would create {len(CROSS_SYSTEM_CLUSTERS)} Jira issues in {project_key}")
        for c in CROSS_SYSTEM_CLUSTERS:
            logger.info(f"  [DRY RUN] Jira issue refs {c['cs']}: {c['jira_summary'][:60]}")
        return len(CROSS_SYSTEM_CLUSTERS)

    try:
        import requests
    except ImportError:
        logger.error("requests library required: pip install requests")
        return 0

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    if user:
        session.auth = (user, token)       # Jira Cloud: basic auth
    else:
        session.headers["Authorization"] = f"Bearer {token}"  # Server/DC: PAT

    created = 0
    for cluster in CROSS_SYSTEM_CLUSTERS:
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": cluster["jira_summary"],
                "description": {
                    "type": "doc", "version": 1,
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": (
                            f"This issue tracks the engineering resolution of "
                            f"Salesforce case {cluster['cs']} "
                            f"(also tracked as ServiceNow {cluster['inc']})."
                        )}]
                    }]
                },
                "issuetype": {"name": "Bug"},
                "labels": ["Salesforce", "cross-system"],
            }
        }
        try:
            resp = session.post(
                f"{jira_url}/rest/api/3/issue",
                json=payload, timeout=15
            )
            resp.raise_for_status()
            issue_key = resp.json().get("key", "")
            state.jira_issue_keys.append(issue_key)
            logger.info(f"  Created Jira {issue_key} refs {cluster['cs']}")
            created += 1
            time.sleep(0.1)
        except Exception as e:
            logger.error(f"  Failed to create Jira issue for {cluster['cs']}: {e}")

    logger.info(f"Jira: {created}/{len(CROSS_SYSTEM_CLUSTERS)} issues created")
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Rollback
# ─────────────────────────────────────────────────────────────────────────────

def rollback(state: SeedState) -> None:
    """Delete all records created by seed_all(). Uses saved sys IDs from seed_state.json."""

    # Salesforce rollback
    if state.sf_case_ids:
        instance_url = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
        access_token = os.getenv("SF_ACCESS_TOKEN", "")
        if instance_url and access_token:
            try:
                import requests
                session = requests.Session()
                session.headers["Authorization"] = f"Bearer {access_token}"
                for sf_id in state.sf_case_ids:
                    try:
                        resp = session.delete(
                            f"{instance_url}/services/data/v59.0/sobjects/Case/{sf_id}",
                            timeout=10
                        )
                        if resp.status_code in (200, 204):
                            logger.info(f"  Deleted SF Case {sf_id}")
                        else:
                            logger.warning(f"  SF Case {sf_id}: HTTP {resp.status_code}")
                    except Exception as e:
                        logger.error(f"  Failed to delete SF Case {sf_id}: {e}")
            except ImportError:
                logger.error("requests required for rollback")

    # ServiceNow rollback
    if state.sn_incident_sys_ids:
        sn_url = os.getenv("SERVICENOW_URL", "").rstrip("/")
        token = os.getenv("SERVICENOW_TOKEN", "")
        user = os.getenv("SERVICENOW_USER", "")
        passwd = os.getenv("SERVICENOW_PASS", "")
        if sn_url:
            try:
                import requests
                session = requests.Session()
                session.headers.update({"Accept": "application/json"})
                if token:
                    session.headers["Authorization"] = f"Bearer {token}"
                elif user:
                    session.auth = (user, passwd)
                for sys_id in state.sn_incident_sys_ids:
                    try:
                        resp = session.delete(
                            f"{sn_url}/api/now/table/incident/{sys_id}",
                            timeout=10
                        )
                        if resp.status_code in (200, 204):
                            logger.info(f"  Deleted SN incident {sys_id[:8]}...")
                        else:
                            logger.warning(f"  SN incident {sys_id[:8]}: HTTP {resp.status_code}")
                    except Exception as e:
                        logger.error(f"  Failed to delete SN incident {sys_id}: {e}")
            except ImportError:
                logger.error("requests required for rollback")

    # Jira rollback
    if state.jira_issue_keys:
        jira_url = os.getenv("JIRA_URL", "").rstrip("/")
        token = os.getenv("JIRA_TOKEN", "")
        user = os.getenv("JIRA_USER", "")
        if jira_url and token:
            try:
                import requests
                session = requests.Session()
                session.headers.update({"Accept": "application/json"})
                if user:
                    session.auth = (user, token)
                else:
                    session.headers["Authorization"] = f"Bearer {token}"
                for key in state.jira_issue_keys:
                    try:
                        resp = session.delete(
                            f"{jira_url}/rest/api/3/issue/{key}",
                            timeout=10
                        )
                        if resp.status_code in (200, 204):
                            logger.info(f"  Deleted Jira {key}")
                        else:
                            logger.warning(f"  Jira {key}: HTTP {resp.status_code}")
                    except Exception as e:
                        logger.error(f"  Failed to delete Jira {key}: {e}")
            except ImportError:
                logger.error("requests required for rollback")

    # Clear state file
    if SEED_STATE_PATH.exists():
        SEED_STATE_PATH.unlink()
        logger.info("Seed state cleared.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def seed_all(
    systems: List[str],
    dry_run: bool = False,
) -> SeedState:
    """Run all seeders for the specified systems. Returns state for inspection."""
    state = SeedState(seeded_at=datetime.now(timezone.utc).isoformat())

    if "sf" in systems or "all" in systems:
        seed_salesforce(state, dry_run=dry_run)
    if "sn" in systems or "all" in systems:
        seed_servicenow(state, dry_run=dry_run)
    if "jira" in systems or "all" in systems:
        seed_jira(state, dry_run=dry_run)

    if not dry_run:
        state.save()

    logger.info(
        f"Seeding complete: {len(state.sf_case_ids)} SF cases, "
        f"{len(state.sn_incident_sys_ids)} SN incidents, "
        f"{len(state.jira_issue_keys)} Jira issues"
    )
    return state


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="AgentIQ demo seeder — seeds cross-system reference data"
    )
    parser.add_argument(
        "--systems", nargs="+",
        choices=["all", "sf", "sn", "jira"],
        default=["all"],
        help="Which systems to seed (default: all)"
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="Delete all previously seeded records"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be created without making API calls"
    )
    args = parser.parse_args()

    if args.rollback:
        state = SeedState.load()
        if not (state.sf_case_ids or state.sn_incident_sys_ids or state.jira_issue_keys):
            logger.info("No seed state found — nothing to roll back.")
        else:
            logger.info(
                f"Rolling back: {len(state.sf_case_ids)} SF, "
                f"{len(state.sn_incident_sys_ids)} SN, "
                f"{len(state.jira_issue_keys)} Jira"
            )
            rollback(state)
    else:
        seed_all(systems=args.systems, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
