"""
SN-CONNECT-1 + JIRA-CONNECT-1 — Live Connector Health Checks
Sprint 5 — Track C
AT-90 — T1-S10-C Scheduled Health Check Job (Sprint 10)

Provides two things:

1. Per-connector health check functions (Sprint 5, unchanged):
     check_servicenow()      → ConnectorHealth
     check_jira()            → ConnectorHealth
     check_ncino()           → ConnectorHealth
     check_strs_benefits()   → ConnectorHealth
     check_all_connectors()  → dict   (called at run start, stored in run KV)

2. AT-90 scheduled job (Sprint 10):
     start_health_check_job()  — call once from app startup
     stop_health_check_job()   — called on graceful shutdown / SIGTERM

   The job runs every CONNECTOR_HEALTH_CHECK_INTERVAL_SECONDS (default 900).
   For every connector in every workspace it calls the relevant check_*
   function and writes a connector.health_check telemetry event via
   record_event() — the only approved write path.

ConnectorHealth fields
----------------------
  status:     "live" | "fixture" | "error"
  system:     "ServiceNow" | "Jira" | "nCino" | "STRS Benefits (PSS)"
  message:    human-readable status message
  latency_ms: round-trip time if live (None if fixture/error)

Telemetry status mapping (AT-90)
---------------------------------
  "live"    → "connected"
  "fixture" → "needs_auth"      (no credentials configured)
  "error"   → "needs_refresh"   (credentials present but check failed)
"""
from __future__ import annotations

import logging
import os
import signal
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.telemetry import record_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (AT-90 T6)
# ---------------------------------------------------------------------------

HEALTH_CHECK_INTERVAL_SECONDS: int = int(
    os.environ.get("CONNECTOR_HEALTH_CHECK_INTERVAL_SECONDS", "900")
)

# ---------------------------------------------------------------------------
# ConnectorHealth dataclass (Sprint 5 — unchanged)
# ---------------------------------------------------------------------------

@dataclass
class ConnectorHealth:
    system:     str
    status:     str           # "live" | "fixture" | "error"
    message:    str
    latency_ms: Optional[int] = None

    @property
    def is_live(self) -> bool:
        return self.status == "live"

    def to_dict(self) -> dict:
        return {
            "system":     self.system,
            "status":     self.status,
            "message":    self.message,
            "latencyMs":  self.latency_ms,
            "isLive":     self.is_live,
        }

    def to_telemetry_status(self) -> str:
        """Map Sprint-5 status → AT-90 telemetry status string.

        "live"    → "connected"      (token valid, API reachable)
        "fixture" → "needs_auth"     (env vars not set, no credentials)
        "error"   → "needs_refresh"  (credentials present but check failed)
        """
        return {
            "live":    "connected",
            "fixture": "needs_auth",
            "error":   "needs_refresh",
        }.get(self.status, "needs_auth")


# ---------------------------------------------------------------------------
# Sprint 5 health check functions (unchanged)
# ---------------------------------------------------------------------------

def check_servicenow() -> ConnectorHealth:
    """
    SN-CONNECT-1: Test ServiceNow connectivity.

    Env vars required for live mode:
      SERVICENOW_URL    e.g. https://myinstance.service-now.com
      SERVICENOW_TOKEN  Bearer token
      (or SERVICENOW_USER + SERVICENOW_PASS for basic auth)

    Health endpoint: GET /api/now/table/incident?sysparm_limit=1
    Returns ConnectorHealth with status "live", "fixture", or "error".
    """
    sn_url   = os.getenv("SERVICENOW_URL", "").rstrip("/")
    sn_token = os.getenv("SERVICENOW_TOKEN", "")
    sn_user  = os.getenv("SERVICENOW_USER", "")
    sn_pass  = os.getenv("SERVICENOW_PASS", "")

    if not sn_url:
        return ConnectorHealth(
            system="ServiceNow",
            status="fixture",
            message="SERVICENOW_URL not set — using fixture data",
        )

    if not sn_token and not (sn_user and sn_pass):
        return ConnectorHealth(
            system="ServiceNow",
            status="fixture",
            message="No credentials set (SERVICENOW_TOKEN or SERVICENOW_USER/PASS) — using fixture data",
        )

    try:
        import requests
    except ImportError:
        return ConnectorHealth(
            system="ServiceNow",
            status="error",
            message="requests library not installed — pip install requests",
        )

    url    = f"{sn_url}/api/now/table/incident"
    params = {"sysparm_limit": "1", "sysparm_fields": "sys_id"}

    if sn_token:
        headers = {"Authorization": f"Bearer {sn_token}", "Accept": "application/json"}
        auth = None
    else:
        headers = {"Accept": "application/json"}
        auth = (sn_user, sn_pass)

    try:
        t0         = time.monotonic()
        resp       = requests.get(url, headers=headers, auth=auth, params=params, timeout=10)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code == 200:
            logger.info("SN-CONNECT-1: ServiceNow live — %dms", latency_ms)
            return ConnectorHealth(
                system="ServiceNow",
                status="live",
                message=f"Connected to {sn_url} — health check passed (shallow check: object reachable + authenticated)",
                latency_ms=latency_ms,
            )
        elif resp.status_code == 401:
            return ConnectorHealth(
                system="ServiceNow",
                status="error",
                message="Authentication failed — check SERVICENOW_TOKEN or credentials",
            )
        elif resp.status_code == 429:
            return ConnectorHealth(
                system="ServiceNow",
                status="error",
                message="Rate limited by ServiceNow — retry later",
            )
        else:
            return ConnectorHealth(
                system="ServiceNow",
                status="error",
                message=f"ServiceNow returned HTTP {resp.status_code}",
            )

    except requests.exceptions.ConnectionError:
        return ConnectorHealth(
            system="ServiceNow",
            status="error",
            message=f"Cannot reach {sn_url} — check SERVICENOW_URL",
        )
    except requests.exceptions.Timeout:
        return ConnectorHealth(
            system="ServiceNow",
            status="error",
            message="ServiceNow health check timed out (10s)",
        )
    except Exception as e:
        logger.warning("SN health check error: %s", e)
        return ConnectorHealth(
            system="ServiceNow",
            status="error",
            message=f"Unexpected error: {e}",
        )


def check_jira() -> ConnectorHealth:
    """
    JIRA-CONNECT-1: Test Jira connectivity.

    Env vars required for live mode:
      JIRA_URL    e.g. https://mycompany.atlassian.net
      JIRA_TOKEN  Personal access token or API token

    Health endpoint: GET /rest/api/3/myself
    Returns ConnectorHealth with status "live", "fixture", or "error".
    """
    jira_url   = os.getenv("JIRA_URL", "").rstrip("/")
    jira_token = os.getenv("JIRA_TOKEN", "")

    if not jira_url:
        return ConnectorHealth(
            system="Jira",
            status="fixture",
            message="JIRA_URL not set — using fixture data",
        )

    if not jira_token:
        return ConnectorHealth(
            system="Jira",
            status="fixture",
            message="JIRA_TOKEN not set — using fixture data",
        )

    try:
        import requests
    except ImportError:
        return ConnectorHealth(
            system="Jira",
            status="error",
            message="requests library not installed — pip install requests",
        )

    url     = f"{jira_url}/rest/api/3/myself"
    headers = {
        "Authorization": f"Bearer {jira_token}",
        "Accept":        "application/json",
    }

    try:
        t0         = time.monotonic()
        resp       = requests.get(url, headers=headers, timeout=10)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code == 200:
            data         = resp.json()
            display_name = data.get("displayName", "authenticated user")
            logger.info("JIRA-CONNECT-1: Jira live — %dms — %s", latency_ms, display_name)
            return ConnectorHealth(
                system="Jira",
                status="live",
                message=f"Connected to {jira_url} as {display_name} — health check passed (shallow check: authenticated)",
                latency_ms=latency_ms,
            )
        elif resp.status_code == 401:
            return ConnectorHealth(
                system="Jira",
                status="error",
                message="Authentication failed — check JIRA_TOKEN",
            )
        elif resp.status_code == 429:
            return ConnectorHealth(
                system="Jira",
                status="error",
                message="Rate limited by Jira — retry later",
            )
        else:
            return ConnectorHealth(
                system="Jira",
                status="error",
                message=f"Jira returned HTTP {resp.status_code}",
            )

    except requests.exceptions.ConnectionError:
        return ConnectorHealth(
            system="Jira",
            status="error",
            message=f"Cannot reach {jira_url} — check JIRA_URL",
        )
    except requests.exceptions.Timeout:
        return ConnectorHealth(
            system="Jira",
            status="error",
            message="Jira health check timed out (10s)",
        )
    except Exception as e:
        logger.warning("Jira health check error: %s", e)
        return ConnectorHealth(
            system="Jira",
            status="error",
            message=f"Unexpected error: {e}",
        )


def check_ncino() -> ConnectorHealth:
    """
    ENG-AIQ-NC-1: Test nCino / Salesforce connectivity.

    Env vars required for live mode:
      SF_INSTANCE_URL    e.g. https://myorg.my.salesforce.com
      SF_ACCESS_TOKEN    OAuth bearer token

    Health endpoint: GET /services/data/v59.0/sobjects/LLC_BI__Loan__c/
    Returns ConnectorHealth with status "live", "fixture", or "error".
    """
    sf_url   = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
    sf_token = os.getenv("SF_ACCESS_TOKEN", "")

    if not sf_url:
        return ConnectorHealth(
            system="nCino",
            status="fixture",
            message="SF_INSTANCE_URL not set - using fixture data",
        )

    if not sf_token:
        return ConnectorHealth(
            system="nCino",
            status="fixture",
            message="SF_ACCESS_TOKEN not set - using fixture data",
        )

    try:
        import requests
    except ImportError:
        return ConnectorHealth(
            system="nCino",
            status="error",
            message="requests library not installed",
        )

    url     = f"{sf_url}/services/data/v59.0/sobjects/LLC_BI__Loan__c/"
    headers = {
        "Authorization": f"Bearer {sf_token}",
        "Accept":        "application/json",
    }

    try:
        t0         = time.monotonic()
        resp       = requests.get(url, headers=headers, timeout=10)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code == 200:
            soql_url = f"{sf_url}/services/data/v59.0/query"
            try:
                soql_resp = requests.get(
                    soql_url,
                    headers=headers,
                    params={"q": "SELECT Id FROM LLC_BI__Loan__c LIMIT 1"},
                    timeout=10,
                )
            except Exception:
                logger.info("ENG-AIQ-NC-1: nCino object reachable - %dms", latency_ms)
                return ConnectorHealth(
                    system="nCino",
                    status="live",
                    message=f"Connected to {sf_url} - LLC_BI__Loan__c object reachable",
                    latency_ms=latency_ms,
                )

            if soql_resp.status_code == 200:
                logger.info("ENG-AIQ-NC-1: nCino live and queryable - %dms", latency_ms)
                return ConnectorHealth(
                    system="nCino",
                    status="live",
                    message=f"Connected to {sf_url} — health check passed (SOQL queryable)",
                    latency_ms=latency_ms,
                )

            return ConnectorHealth(
                system="nCino",
                status="error",
                message=(
                    f"LLC_BI__Loan__c object exists but SOQL query failed "
                    f"(HTTP {soql_resp.status_code}) - check field-level permissions"
                ),
            )

        if resp.status_code == 401:
            return ConnectorHealth(
                system="nCino",
                status="error",
                message="Authentication failed - check SF_ACCESS_TOKEN",
            )
        if resp.status_code == 404:
            return ConnectorHealth(
                system="nCino",
                status="error",
                message="LLC_BI__Loan__c not found - nCino may not be installed on this org",
            )
        if resp.status_code == 429:
            return ConnectorHealth(
                system="nCino",
                status="error",
                message="Rate limited by Salesforce - retry later",
            )

        return ConnectorHealth(
            system="nCino",
            status="error",
            message=f"Salesforce returned HTTP {resp.status_code}",
        )

    except requests.exceptions.ConnectionError:
        return ConnectorHealth(
            system="nCino",
            status="error",
            message=f"Cannot reach {sf_url} - check SF_INSTANCE_URL",
        )
    except requests.exceptions.Timeout:
        return ConnectorHealth(
            system="nCino",
            status="error",
            message="nCino health check timed out (10s)",
        )
    except Exception as e:
        logger.warning("nCino health check error: %s", e)
        return ConnectorHealth(
            system="nCino",
            status="error",
            message=f"Unexpected error: {e}",
        )


def check_strs_benefits() -> ConnectorHealth:
    """
    ENG-STRS-1: Test STRS Benefits Administration / PSS Salesforce connectivity.

    Env vars required for live mode (same as nCino — same Salesforce org):
      SF_INSTANCE_URL    e.g. https://myorg.my.salesforce.com
      SF_ACCESS_TOKEN    OAuth bearer token

    Health check: probes IndividualApplication object reachability via
    SOQL smoke query. Same pattern as check_ncino().

    Returns ConnectorHealth with status "live", "fixture", or "error".
    """
    sf_url   = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
    sf_token = os.getenv("SF_ACCESS_TOKEN", "")

    if not sf_url:
        return ConnectorHealth(
            system="STRS Benefits (PSS)",
            status="fixture",
            message="SF_INSTANCE_URL not set — using fixture data",
        )

    if not sf_token:
        return ConnectorHealth(
            system="STRS Benefits (PSS)",
            status="fixture",
            message="SF_ACCESS_TOKEN not set — using fixture data",
        )

    try:
        import requests
    except ImportError:
        return ConnectorHealth(
            system="STRS Benefits (PSS)",
            status="error",
            message="requests library not installed — pip install requests",
        )

    url     = f"{sf_url}/services/data/v59.0/sobjects/IndividualApplication/"
    headers = {
        "Authorization": f"Bearer {sf_token}",
        "Accept":        "application/json",
    }

    try:
        t0         = time.monotonic()
        resp       = requests.get(url, headers=headers, timeout=10)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code != 200:
            return ConnectorHealth(
                system="STRS Benefits (PSS)",
                status="error",
                message=(
                    f"IndividualApplication not accessible — HTTP {resp.status_code}. "
                    "Confirm PSS is installed in this org."
                ),
            )

        soql_url  = (
            f"{sf_url}/services/data/v59.0/query"
            f"?q=SELECT+Id,Status,NextPayoutDate+FROM+BenefitAssignment+LIMIT+1"
        )
        soql_resp = requests.get(soql_url, headers=headers, timeout=10)

        if not soql_resp.ok:
            return ConnectorHealth(
                system="STRS Benefits (PSS)",
                status="error",
                message=(
                    f"BenefitAssignment SOQL failed — HTTP {soql_resp.status_code}. "
                    "Confirm PSS BenefitAssignment object is accessible."
                ),
            )

        return ConnectorHealth(
            system="STRS Benefits (PSS)",
            status="live",
            message=(
                f"Connected to {sf_url} — health check passed "
                "(IndividualApplication reachable, BenefitAssignment SOQL queryable)"
            ),
            latency_ms=latency_ms,
        )

    except Exception as exc:
        return ConnectorHealth(
            system="STRS Benefits (PSS)",
            status="error",
            message=f"Connection failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Sprint 5 — check_all_connectors (FIXED: now includes STRS Benefits)
# ---------------------------------------------------------------------------

# Maps system name → check function.  Add new connectors here.
_CONNECTOR_CHECKS = {
    "ServiceNow":        check_servicenow,
    "Jira":              check_jira,
    "nCino":             check_ncino,
    "STRS Benefits (PSS)": check_strs_benefits,
}


def check_all_connectors() -> dict:
    """Run health checks for all configured connectors.

    Returns dict keyed by system name.
    Called at run start — results stored in run KV under 'connector_health'.

    FIX (AT-90): check_strs_benefits() was missing from the original Sprint 5
    implementation.  Now driven by _CONNECTOR_CHECKS so future connectors only
    need a single registration point.
    """
    return {name: fn().to_dict() for name, fn in _CONNECTOR_CHECKS.items()}


# ---------------------------------------------------------------------------
# AT-90 — Scheduled telemetry job (Sprint 10)
# ---------------------------------------------------------------------------

# Maps system name → connector_id written into the telemetry event.
# These are stable identifiers, not display names.
_CONNECTOR_IDS = {
    "ServiceNow":          "servicenow",
    "Jira":                "jira",
    "nCino":               "ncino",
    "STRS Benefits (PSS)": "strs_benefits",
}

# Module-level scheduler — one instance for the process lifetime.
_scheduler: Optional[BackgroundScheduler] = None


def _check_and_record(name: str, org_id: str) -> None:
    """Run one connector's check function and write a telemetry event.

    Exceptions are caught and logged — one failing connector must never
    abort checks for the remaining connectors.

    Args:
        name:   System name key from _CONNECTOR_CHECKS.
        org_id: Workspace org_id that owns this connector.
    """
    try:
        check_fn     = _CONNECTOR_CHECKS[name]
        t0           = time.monotonic()
        result       = check_fn()
        duration_ms  = int((time.monotonic() - t0) * 1000)
        connector_id = _CONNECTOR_IDS.get(name, name.lower().replace(" ", "_"))

        # Locked signature is record_event(event_type, payload) (T3-S10-A);
        # org_id/source/connector_id travel inside the payload, where
        # record_event() extracts them. Passing them as keyword arguments
        # raises TypeError and silently drops the event (AT-209).
        record_event(
            "connector.health_check",
            {
                "status":               result.to_telemetry_status(),
                "connector_id":         connector_id,
                # token_expiry_seconds not available from these env-based connectors;
                # set to None.  OAuth-based connectors in future sprints will populate this.
                "token_expiry_seconds": None,
                "check_duration_ms":    duration_ms,
                "org_id":               org_id,
                "source":               "connector",
            },
        )

        logger.debug(
            "AT-90 health_check org=%s connector=%s status=%s duration_ms=%d",
            org_id,
            connector_id,
            result.to_telemetry_status(),
            duration_ms,
        )

    except Exception:
        logger.error(
            "AT-90 health check failed — connector=%s org=%s\n%s",
            name,
            org_id,
            traceback.format_exc(),
        )


def _get_all_org_ids() -> list[str]:
    """Return all active org_ids in the system.

    Tries to import from the workspace repository.  Falls back to the
    AGENTIQ_ORG_ID env var so the job works in single-tenant / local dev
    without a full DB.
    """
    try:
        from repositories.workspace import WorkspaceRepository  # adjust path if needed
        return [ws.org_id for ws in WorkspaceRepository.get_all()]
    except Exception:
        logger.warning(
            "AT-90: WorkspaceRepository unavailable — falling back to AGENTIQ_ORG_ID env var"
        )
        fallback = os.environ.get("AGENTIQ_ORG_ID", "")
        return [fallback] if fallback else []


def run_connector_health_checks() -> None:
    """Main job function — iterate all orgs × all connectors, write telemetry.

    Called by APScheduler on the configured interval.  Also safe to call
    directly in tests or for a manual one-shot run.
    """
    logger.info(
        "AT-90 connector health check starting at %s",
        datetime.now(timezone.utc).isoformat(),
    )

    org_ids = _get_all_org_ids()
    if not org_ids:
        logger.warning("AT-90 health check: no org_ids found — skipping")
        return

    for org_id in org_ids:
        for name in _CONNECTOR_CHECKS:
            _check_and_record(name=name, org_id=org_id)

    logger.info(
        "AT-90 connector health check complete at %s",
        datetime.now(timezone.utc).isoformat(),
    )


def start_health_check_job() -> None:
    """Start the APScheduler background scheduler.  Call once from app startup.

    Example (main.py / app.py)::

        from app.connector_health import start_health_check_job
        start_health_check_job()

    The job fires immediately on startup (next_run_time=now()), then every
    CONNECTOR_HEALTH_CHECK_INTERVAL_SECONDS seconds.
    SIGTERM is wired to stop_health_check_job() for graceful shutdown.
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.warning("AT-90: connector health check scheduler already running — skipping")
        return

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func=run_connector_health_checks,
        trigger="interval",
        seconds=HEALTH_CHECK_INTERVAL_SECONDS,
        id="connector_health_check",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),   # fire immediately on startup
    )
    _scheduler.start()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    logger.info(
        "AT-90: connector health check scheduler started — interval=%ds",
        HEALTH_CHECK_INTERVAL_SECONDS,
    )


def stop_health_check_job() -> None:
    """Stop the scheduler gracefully.  Safe to call if not running."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("AT-90: connector health check scheduler stopped")
    _scheduler = None


def _sigterm_handler(signum: int, frame: object) -> None:
    logger.info("SIGTERM received — stopping AT-90 connector health check scheduler")
    stop_health_check_job()