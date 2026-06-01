"""
ENG-SHARED-1 — Pack Configuration Architecture
Sprint 5 — Wave 3

Provides a configuration-driven pack system so AgentIQ can run as either:
  - service_cloud  (default — existing Service Cloud detectors)
  - ncino          (nCino lending detectors + banking language)

A pack config defines:
  packId:           unique identifier
  packName:         human-readable name
  domain:           "service_cloud" | "ncino"
  detectors:        list of detector module paths to activate
  pack_domain:      passed to enrich_ambiguous_mappings() for entity gating
  ui_labels_path:   path to JSON file with S6/S7/S9/S10 labels (optional)
  llm_context:      context hint for LLM enrichment prompt

SHARED-1 replaces the temporary is_ncino_pack conditional used in
AIQ-NC-4 and AIQ-NC-5 while this story was pending.

CPQ pack slot is reserved but empty — Sprint 6 adds ncino_cpq.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Pack registry ─────────────────────────────────────────────────────────────

_PACKS_DIR = Path(__file__).parent

PACK_REGISTRY: Dict[str, Dict[str, Any]] = {

    "service_cloud": {
        "packId":        "service_cloud",
        "packName":      "Service Cloud",
        "domain":        "service_cloud",
        "pack_domain":   "service_cloud",
        "detectors": [
            "discovery.detectors.repetition",
            "discovery.detectors.handoff_friction",
            "discovery.detectors.approval_delay",
            "discovery.detectors.knowledge_gap",
            "discovery.detectors.integration_concentration",
            "discovery.detectors.permission_bottleneck",
            "discovery.detectors.cross_system_echo",
        ],
        "ui_labels_path": None,
        "llm_context": (
            "Service Cloud implementation analysis. "
            "Focus on case management, flow automation, and approval workflows."
        ),
    },

    "ncino": {
        "packId":        "ncino",
        "packName":      "nCino Lending",
        "domain":        "ncino",
        "pack_domain":   "ncino",
        "detectors": [
            "discovery.detectors.loan_origination_routing_friction",
            "discovery.detectors.covenant_tracking_gap", 
            "discovery.detectors.checklist_bottleneck",
            "discovery.detectors.spreading_bottleneck",
            "discovery.detectors.approval_bottleneck", 
        ],
        "ui_labels_path": str(_PACKS_DIR / "ncino_ui_labels.json"),
        "llm_context": (
            "nCino commercial lending analysis. "
            "Focus on loan origination friction, covenant compliance gaps, "
            "document checklist bottlenecks, financial spreading delays, "
            "and approval cycle time. "
            "Use banking operations language — not Salesforce admin language. "
            "IMPORTANT: never suggest automated credit decisions. "
            "All credit decisions require human approval."
        ),
    },


    "strs_benefits": {
        "packId":        "strs_benefits",
        "packName":      "STRS Benefits Administration",
        "domain":        "strs_benefits",
        "pack_domain":   "strs_benefits",
        "detectors": [
            "discovery.detectors.application_stall",
            "discovery.detectors.benefit_election_deadline",
            "discovery.detectors.disbursement_overdue",
            "discovery.detectors.disability_review_bottleneck",
        ],
        "ui_labels_path": str(_PACKS_DIR / "strs_benefits_ui_labels.json"),
        "llm_context": (
            "STRS public sector pension fund benefit administration analysis. "
            "Focus on retirement application processing delays, benefit election "
            "deadline misses, disbursement overdue situations, and disability "
            "review bottlenecks. "
            "Use member services language — not Salesforce admin language. "
            "Reference Ohio Revised Code 3307 obligations where relevant. "
            "IMPORTANT: agent surfaces alerts to member services staff only. "
            "No automated benefit decisions. All benefit actions require human approval."
        ),
    },

    "sqlserver_opsignal": {
        "packId":        "sqlserver_opsignal",
        "packName":      "SQL Server Operational Signals",
        "domain":        "sqlserver_opsignal",
        "pack_domain":   "sqlserver_opsignal",
        "detectors": [
            "discovery.detectors.db_ticket_volume_surge",
            "discovery.detectors.db_sla_breach_rate",
            "discovery.detectors.db_queue_depth_elevated",
        ],
        "ui_labels_path": str(_PACKS_DIR / "sqlserver_opsignal_ui_labels.json"),
        "llm_context": (
            "SQL Server operational database analysis. "
            "Focus on service ticket volume trends, SLA breach patterns, "
            "and queue depth accumulation. "
            "Use IT operations language. "
            "Cross-reference with ServiceNow and Jira findings where available. "
            "IMPORTANT: agent surfaces operational signals to IT managers only. "
            "No automated ticket resolution or SLA override."
        ),
    },

    # CPQ pack slot — reserved for Sprint 6

    # "ncino_cpq": {
    #     "packId":   "ncino_cpq",
    #     "packName": "nCino CPQ",
    #     "domain":   "ncino",
    #     ...
    # },
}

DEFAULT_PACK = "service_cloud"


# ── Public API ────────────────────────────────────────────────────────────────

def get_pack(pack_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Return the pack config for pack_id.
    Falls back to DEFAULT_PACK if pack_id is None or unknown.

    This is the single entry point for pack selection — replaces all
    temporary is_ncino_pack conditionals in AIQ-NC-4 and AIQ-NC-5.
    """
    if pack_id and pack_id in PACK_REGISTRY:
        return PACK_REGISTRY[pack_id]
    return PACK_REGISTRY[DEFAULT_PACK]


def get_pack_domain(pack_id: Optional[str] = None) -> str:
    """Return the pack_domain string for use with enrich_ambiguous_mappings()."""
    return get_pack(pack_id)["pack_domain"]


def get_detector_modules(pack_id: Optional[str] = None) -> List[str]:
    """Return list of detector module paths for this pack."""
    return get_pack(pack_id)["detectors"]


def get_ui_labels(pack_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Return the UI labels dict for this pack, or None if no labels file.
    Used by AIQ-NC-5 to populate S6/S7/S9/S10 screen text.
    """
    pack = get_pack(pack_id)
    labels_path = pack.get("ui_labels_path")
    if not labels_path:
        return None
    try:
        with open(labels_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_llm_context(pack_id: Optional[str] = None) -> str:
    """Return the LLM context hint string for this pack."""
    return get_pack(pack_id)["llm_context"]




def is_strs_benefits_pack(pack_id: Optional[str] = None) -> bool:
    """
    Returns True when the active pack is STRS Benefits Administration.
    Used in runner.py, scorer, and evidence_builder for pack routing.
    """
    return get_pack(pack_id)["domain"] == "strs_benefits"


def list_packs() -> List[str]:
    """Return all registered pack IDs."""
    return list(PACK_REGISTRY.keys())


def is_ncino_pack(pack_id: Optional[str] = None) -> bool:
    """
    Convenience helper — replaces the temporary is_ncino_pack conditional.
    Returns True when the active pack is nCino domain.
    """
    return get_pack(pack_id)["domain"] == "ncino"


def is_sqlserver_opsignal_pack(pack_id: Optional[str] = None) -> bool:
    """Returns True when the active pack is SQL Server Operational Signals."""
    return get_pack(pack_id)["domain"] == "sqlserver_opsignal"
 