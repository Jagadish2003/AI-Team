from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import is_live

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "salesforce_sample.json"

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
# Live HTTP helpers (Updated for SME Public URL)
# ─────────────────────────────────────────────────────────────────────────────

def _get_client() -> "SalesforceClient":
    """Build a minimal REST client using the Public URL."""
    instance_url = os.getenv("SF_INSTANCE_URL", "")
    if not instance_url:
        raise IngestError(
            "Live mode requires SF_INSTANCE_URL to be set to the SME Public URL. "
            "Set INGEST_MODE=offline to run without it."
        )
    return SalesforceClient(instance_url)

class SalesforceClient:
    """Thin wrapper to fetch pre-calculated JSON from the Public URL."""
    def __init__(self, instance_url: str):
        self.instance_url = instance_url
        self._public_data = None

    def fetch_public_data(self) -> Dict[str, Any]:
        if self._public_data is None:
            try:
                import requests
                resp = requests.get(self.instance_url, timeout=30)
                resp.raise_for_status()
                payload = resp.json()
                self._public_data = payload.get("data", {})
            except ImportError:
                raise IngestError("requests library required for live mode: pip install requests")
            except Exception as e:
                raise IngestError(f"Failed to fetch JSON from Public URL: {e}")
        return self._public_data

# ─────────────────────────────────────────────────────────────────────────────
# Seven ingestion functions (Now extracting from public JSON)
# ─────────────────────────────────────────────────────────────────────────────

def get_case_metrics(client: Optional[SalesforceClient] = None) -> Dict[str, Any]:
    if not is_live(): return _load_fixture().get("case_metrics", {})
    return client.fetch_public_data().get("case_metrics", {})

def get_flow_inventory(client: Optional[SalesforceClient] = None) -> Dict[str, Any]:
    if not is_live(): return _load_fixture().get("flow_inventory", {})
    return client.fetch_public_data().get("flow_inventory", {})

def get_approval_pending(client: Optional[SalesforceClient] = None) -> List[Dict[str, Any]]:
    if not is_live(): return _load_fixture().get("approval_processes",[])
    return client.fetch_public_data().get("approval_processes", [])

def get_knowledge_coverage(client: Optional[SalesforceClient] = None) -> Dict[str, Any]:
    if not is_live():
        cm = _load_fixture().get("case_metrics", {})
    else:
        cm = client.fetch_public_data().get("case_metrics", {})

    return {
        "closed_cases_90d": cm.get("closed_cases_90d", 0),
        "cases_with_kb_link": cm.get("cases_with_kb_link", 0),
        "knowledge_gap_score": cm.get("knowledge_gap_score", 0.0),
    }

def get_named_credentials(client: Optional[SalesforceClient] = None) -> List[Dict[str, Any]]:
    if not is_live(): return _load_fixture().get("named_credentials", [])
    return client.fetch_public_data().get("named_credentials",[])

def get_named_credential_flow_refs(
    named_credentials: List[Dict[str, Any]],
    client: Optional[SalesforceClient] = None,
) -> List[Dict[str, Any]]:
    if not is_live(): return named_credentials
    # The public JSON already includes 'flow_reference_count', so we just return the block
    return client.fetch_public_data().get("named_credentials",[])

def get_permission_bottlenecks(client: Optional[SalesforceClient] = None) -> List[Dict[str, Any]]:
    return get_approval_pending(client)

def get_cross_system_references(
    client: Optional[SalesforceClient] = None,
    patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if not is_live(): return _load_fixture().get("cross_system_references", {})
    return client.fetch_public_data().get("cross_system_references", {})

# ─────────────────────────────────────────────────────────────────────────────
# Main ingest() — called by runner.py
# ─────────────────────────────────────────────────────────────────────────────

def ingest(sf_client: Optional[SalesforceClient] = None) -> Dict[str, Any]:
    if not is_live():
        logger.info("Salesforce ingestion: offline mode (fixture)")
        return _load_fixture()

    logger.info("Salesforce ingestion: live mode (Public URL)")
    if sf_client is None:
        sf_client = _get_client()

    try:
        def _timed(fn_name, fn_call):
            """Execute an ingestion function with timing."""
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

        # Pre-fetch the payload once so all functions are instant
        sf_client.fetch_public_data()

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
