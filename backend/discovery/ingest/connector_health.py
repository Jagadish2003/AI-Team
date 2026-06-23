"""
SN-CONNECT-1 + JIRA-CONNECT-1 — Live Connector Health Checks
Sprint 5 — Track C

Provides connection health check functions for ServiceNow, Jira, and nCino.
Called at run start to determine:
  - whether live credentials are configured
  - whether the remote API is reachable
  - what status badge to show on S1

Returns a ConnectorHealth object with:
  status:    "live" | "fixture" | "error"
  system:    "ServiceNow" | "Jira" | "nCino"
  message:   human-readable status message
  latency_ms: round-trip time if live (None if fixture/error)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


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


def check_servicenow() -> ConnectorHealth:
    """
    SN-CONNECT-1: Test ServiceNow connectivity.

    Env vars required for live mode (OAuth-only):
      SERVICENOW_URL    e.g. https://myinstance.service-now.com
      SERVICENOW_TOKEN  OAuth Bearer token

    Health endpoint: GET /api/now/table/incident?sysparm_limit=1
    Returns ConnectorHealth with status "live", "fixture", or "error".
    """
    sn_url = os.getenv("SERVICENOW_URL", "").rstrip("/")
    sn_token = os.getenv("SERVICENOW_TOKEN", "")

    if not sn_url:
        return ConnectorHealth(
            system="ServiceNow",
            status="fixture",
            message="SERVICENOW_URL not set — using fixture data",
        )

    if not sn_token:
        return ConnectorHealth(
            system="ServiceNow",
            status="fixture",
            message="No credentials set (SERVICENOW_TOKEN) — using fixture data",
        )

    try:
        import requests
    except ImportError:
        return ConnectorHealth(
            system="ServiceNow",
            status="error",
            message="requests library not installed — pip install requests",
        )

    url = f"{sn_url}/api/now/table/incident"
    params = {"sysparm_limit": "1", "sysparm_fields": "sys_id"}

    headers = {"Authorization": f"Bearer {sn_token}", "Accept": "application/json"}

    try:
        t0 = time.monotonic()
        resp = requests.get(url, headers=headers, params=params, timeout=10)
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
    jira_url = os.getenv("JIRA_URL", "").rstrip("/")
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

    url = f"{jira_url}/rest/api/3/myself"
    headers = {
        "Authorization": f"Bearer {jira_token}",
        "Accept": "application/json",
    }

    try:
        t0 = time.monotonic()
        resp = requests.get(url, headers=headers, timeout=10)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code == 200:
            data = resp.json()
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
    sf_url = os.getenv("SF_INSTANCE_URL", "").rstrip("/")
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

    url = f"{sf_url}/services/data/v59.0/sobjects/LLC_BI__Loan__c/"
    headers = {
        "Authorization": f"Bearer {sf_token}",
        "Accept": "application/json",
    }

    try:
        t0 = time.monotonic()
        resp = requests.get(url, headers=headers, timeout=10)
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


def check_all_connectors() -> dict:
    """
    Run health checks for all configured connectors.
    Returns dict keyed by system name.
    Called at run start — results stored in run KV under 'connector_health'.
    """
    sn = check_servicenow()
    jira = check_jira()
    ncino = check_ncino()
    return {
        "ServiceNow": sn.to_dict(),
        "Jira": jira.to_dict(),
        "nCino": ncino.to_dict(),
    }

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

    # Probe 1: IndividualApplication object reachability
    url     = f"{sf_url}/services/data/v59.0/sobjects/IndividualApplication/"
    headers = {
        "Authorization": f"Bearer {sf_token}",
        "Accept":        "application/json",
    }

    try:
        t0   = time.monotonic()
        resp = requests.get(url, headers=headers, timeout=10)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code != 200:
            return ConnectorHealth(
                system="STRS Benefits (PSS)",
                status="error",
                message=f"IndividualApplication not accessible — HTTP {resp.status_code}. "
                        "Confirm PSS is installed in this org.",
            )

        # Probe 2: SOQL smoke query on BenefitAssignment
        soql_url = (
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
