"""
SF-2.2 stub — Salesforce ingestion module.
Offline mode reads backend/discovery/ingest/fixtures/salesforce_sample.json.
Live mode (SF-2.2 implementation) calls Salesforce REST/SOQL/Tooling APIs.
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from . import is_live

logger = logging.getLogger(__name__)
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "salesforce_sample.json"


def _load_fixture() -> Dict[str, Any]:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def ingest(sf_client=None) -> Dict[str, Any]:
    """
    Return all Tier A Salesforce signals.
    Shape: see fixtures/salesforce_sample.json for the full schema.
    SF-2.2 replaces the live branch with real SOQL/Tooling API calls.
    """
    if is_live():
        # SF-2.2 implements this branch
        raise NotImplementedError("Live Salesforce ingestion — implement in SF-2.2")
    logger.info("Salesforce ingestion: offline mode")
    return _load_fixture()
