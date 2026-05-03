"""
SN-CONNECT-1 + JIRA-CONNECT-1 — Live Connector Health Checks
Sprint 5 — Track C

Provides connection health check functions for ServiceNow and Jira.
Called at run start to determine:
  - whether live credentials are configured
  - whether the remote API is reachable
  - what status badge to show on S1

Returns a ConnectorHealth object with:
  status:    "live" | "fixture" | "error"
  system:    "ServiceNow" | "Jira"
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

    Env vars required for live mode:
      SERVICENOW_URL    e.g. https://myinstance.service-now.com
      SERVICENOW_TOKEN  Bearer token
      (or SERVICENOW_USER + SERVICENOW_PASS for basic auth)

    Health endpoint: GET /api/now/table/incident?sysparm_limit=1
    Returns ConnectorHealth with status "live", "fixture", or "error".
    """
    sn_url = os.getenv("SERVICENOW_URL", "").rstrip("/")
    sn_token = os.getenv("SERVICENOW_TOKEN", "")
    sn_user = os.getenv("SERVICENOW_USER", "")
    sn_pass = os.getenv("SERVICENOW_PASS", "")

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

    url = f"{sn_url}/api/now/table/incident"
    params = {"sysparm_limit": "1", "sysparm_fields": "sys_id"}

    if sn_token:
        headers = {"Authorization": f"Bearer {sn_token}", "Accept": "application/json"}
        auth = None
    else:
        headers = {"Accept": "application/json"}
        auth = (sn_user, sn_pass)

    try:
        t0 = time.monotonic()
        resp = requests.get(url, headers=headers, auth=auth, params=params, timeout=10)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code == 200:
            logger.info("SN-CONNECT-1: ServiceNow live — %dms", latency_ms)
            return ConnectorHealth(
                system="ServiceNow",
                status="live",
                message=f"Connected to {sn_url}",
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
                message=f"Connected to {jira_url} as {display_name}",
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


def check_all_connectors() -> dict:
    """
    Run health checks for both connectors.
    Returns dict keyed by system name.
    Called at run start — results stored in run KV under 'connector_health'.
    """
    sn = check_servicenow()
    jira = check_jira()
    return {
        "ServiceNow": sn.to_dict(),
        "Jira": jira.to_dict(),
    }
