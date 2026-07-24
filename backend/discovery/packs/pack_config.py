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
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Pack registry ─────────────────────────────────────────────────────────────

_PACKS_DIR = Path(__file__).parent

PACK_REGISTRY: Dict[str, Dict[str, Any]] = {

    "service_cloud": {
        "packId":        "service_cloud",
        "packVersion":   "1.0.0",
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
        "packVersion":   "1.0.1",
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
        "packVersion":   "1.0.0",
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
        "packVersion":   "1.0.0",
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

    "github_engineering": {
        "packId":        "github_engineering",
        "packVersion":   "1.0.0",
        "packName":      "GitHub Engineering Signals",
        "domain":        "github_engineering",
        "pack_domain":   "github_engineering",
        "detectors": [
            "discovery.detectors.github_pr_bottleneck",
            "discovery.detectors.github_commit_concentration",
            "discovery.detectors.github_stale_branches",
        ],
        "ui_labels_path": str(_PACKS_DIR / "github_engineering_ui_labels.json"),
        "llm_context": (
            "GitHub engineering signal analysis. "
            "Focus on PR review bottlenecks, commit concentration risk, "
            "and stale branch accumulation. "
            "Use engineering operations language. "
            "Cross-reference with Jira open issues where available for "
            "confidence elevation. "
            "IMPORTANT: agent surfaces signals to engineering leads only. "
            "No automated merge approvals, branch deletions, or code changes."
        ),
    },

    "enterprise_ops": {
        "packId":        "enterprise_ops",
        "packVersion":   "1.0.0",
        "packName":      "Enterprise Operations Intelligence",
        "domain":        "enterprise_ops",
        "pack_domain":   "enterprise_ops",
        "detectors": [
            "discovery.detectors.ent_incident_resolution_lag",
            "discovery.detectors.ent_change_incident_correlation",
            "discovery.detectors.ent_sla_breach_by_team",
        ],
        "ui_labels_path": str(_PACKS_DIR / "enterprise_ops_ui_labels.json"),
        "llm_context": (
            "Enterprise operations intelligence from cross-system analysis of "
            "ServiceNow and Jira. These findings are not visible from either system "
            "alone — they emerge from the gap between incident management and issue "
            "tracking. Use operations leadership language. Avoid IT jargon. "
            "The audience is a VP of Operations or Chief Operating Officer, "
            "not an IT manager. "
            "Focus on organisational impact and team dynamics, "
            "not system configuration or process compliance."
        ),
    },

    "cloud_ops": {
        "packId":        "cloud_ops",
        # MSP-B6 T1 AC1: version stamped on every run (R16-B1). Bumped to 1.1.0 by
        # MSP-B6 T4 (AT-739) — adds the config-driven ops-impact scorer (behaviour
        # change). MSP-B5 production wiring adds its documentation-gap detector.
        "packVersion":   "1.2.0",
        "packName":      "Cloud Operations",
        "domain":        "cloud_ops",
        "pack_domain":   "cloud_ops",
        # MSP-B6 T2 (AT-737) record/stream detectors + T3 (AT-738) shared-CI hotspot.
        "detectors": [
            "discovery.detectors.cloud_ops_recurring_resolution_loop",
            "discovery.detectors.cloud_ops_alert_triage_toil",
            "discovery.detectors.cloud_ops_reassignment_ping_pong",
            "discovery.detectors.cloud_ops_queue_ageing",
            "discovery.detectors.cloud_ops_shared_ci_hotspot",
            "discovery.detectors.cloud_ops_runbook_documentation_gap",
        ],
        # UI labels are per-detector (S6/S7); added with the detectors in T2/T3.
        "ui_labels_path": None,
        # MSP-B6 T1 AC2/AC3: calibration values, thresholds, and the NOC
        # terminology set load from this external file, not from code — a config
        # change alters behaviour with no code deploy. See cloud_ops_config.py.
        "config_path":   str(_PACKS_DIR / "cloud_ops_pack_config.json"),
        "llm_context": (
            "Managed cloud-operations (NOC) analysis. "
            "Speak NOC language: alerts, incidents, runbooks, MTTR, toil, escalation. "
            "Focus on recurring resolution loops, alert-triage toil, reassignment "
            "ping-pong between groups and queues, queue ageing against baseline, and "
            "incidents concentrating on a shared dependency. "
            "Reference groups, queues, services, and CIs only — never individuals. "
            "State corroboration honestly: a single-source finding is capped and "
            "labelled; concentration is described as 'incidents concentrate on...', "
            "never as causation. "
            "IMPORTANT: agent surfaces operational findings to NOC and operations "
            "leaders only. No automated incident remediation, ticket resolution, or "
            "runbook execution — humans remain responsible for every action."
        ),
    },

    "security_ops": {
        "packId":        "security_ops",
        # MSP-B12 version stamped on every run (R16-B1 §4). T1 shipped the scaffold
        # at 1.0.0; MSP-B12 T2 adds the five Section-1 detectors (a behaviour change)
        # → bumped to 1.1.0. Bump on ANY future detector or scorer/calibration (T6)
        # change — an intentional pack-version update is required so pack governance
        # (1.9) can tell a data change from a pack-logic change. A boundary test
        # guards this: it fails if the detector/scoring surface changes without a bump.
        "packVersion":   "1.2.0",
        "packName":      "Security Operations",
        "domain":        "security_ops",
        "pack_domain":   "security_ops",
        # Second sibling of the Cloud-Operations pack on the same template model.
        # MSP-B12 T2 (Section 1) detectors — consume only MSP-B11's SecOps workflow
        # signal (sn_data['secops'] / ['vulnerability_response']) + B3's
        # sn_data['cmdb']. Invoked via the runner's uniform pack-dispatch branch.
        "detectors": [
            "discovery.detectors.security_ops_remediation_recurrence",
            "discovery.detectors.security_ops_security_it_pingpong",
            "discovery.detectors.security_ops_sla_deferral_ageing",
            "discovery.detectors.security_ops_shared_infra_concentration",
            "discovery.detectors.security_ops_sir_triage_toil",
        ],
        # UI labels are per-detector (S6/S7); added with the detectors in T2.
        "ui_labels_path": None,
        # Calibration values, detector thresholds, and the SecOps terminology set
        # load from this external file, not from code — a config change alters
        # behaviour with no code deploy (see security_ops_config.py).
        "config_path":   str(_PACKS_DIR / "security_ops_pack_config.json"),
        # Model-context hint for LLM enrichment. Security-derived content only
        # participates in AI-assisted assembly under in-boundary / customer-tenant
        # model modes; under hosted-AI mode the pack runs deterministic detectors and
        # emits findings with an explicit "AI-assisted narrative unavailable" label
        # (the AI-mode gate is MSP-B12 T4). Vulnerability workload data never leaves
        # the boundary for AI — for federal deployments that is the selling point.
        "llm_context": (
            "Security Operations (SecOps) analysis for a managed-security provider. "
            "Speak SecOps language: remediation, scan cycle, deferral, SLA, triage, "
            "severity band, security queue, CI class. "
            "Focus on recurring vulnerability-remediation loops, security<->IT "
            "reassignment friction, SLA and deferral ageing, and vulnerability "
            "workload concentrating on shared infrastructure. "
            "Describe WORKLOAD — effort, recurrence, ageing, concentration — "
            "aggregated by vulnerability class, service, and CI class. "
            "Reference groups, queues, services, vulnerability classes, and CI "
            "classes ONLY — never an individual employee, an individual host, or a "
            "host x vulnerability pair. "
            "State corroboration honestly: a single-source finding is capped and "
            "labelled; concentration is described as 'workload concentrates on...', "
            "never as causation. "
            "IMPORTANT: agent surfaces findings to SOC and remediation leaders only. "
            "No automated remediation, patching, deferral approval, or exception "
            "sign-off — humans remain responsible for every action."
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

# R16-B1 §4: the version of a pack's detector/scoring logic, stamped onto every
# opportunity instance so pack governance (1.9) and debugging can later tell
# whether a changed output came from changed DATA or a changed PACK VERSION.
# Bump a pack's "packVersion" in PACK_REGISTRY whenever its detector or scoring
# logic changes. DEFAULT_PACK_VERSION is the fallback for a pack that has not
# declared one explicitly.
DEFAULT_PACK_VERSION = "1.0.0"


# ── Public API ────────────────────────────────────────────────────────────────

def get_pack(pack_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Return the pack config for pack_id.
    Falls back to DEFAULT_PACK if pack_id is None or unknown.

    This is the single entry point for pack selection — replaces all
    temporary is_ncino_pack conditionals in AIQ-NC-4 and AIQ-NC-5.

    An unrecognized non-None pack_id logs a WARNING so misconfiguration is
    visible in logs rather than silently producing wrong detector results.
    """
    if pack_id and pack_id in PACK_REGISTRY:
        return PACK_REGISTRY[pack_id]
    if pack_id is not None:
        logger.warning(
            "get_pack: unrecognized pack_id %r — falling back to '%s'. "
            "Valid pack IDs: %s",
            pack_id, DEFAULT_PACK, sorted(PACK_REGISTRY),
        )
    return PACK_REGISTRY[DEFAULT_PACK]


def get_pack_domain(pack_id: Optional[str] = None) -> str:
    """Return the pack_domain string for use with enrich_ambiguous_mappings()."""
    return get_pack(pack_id)["pack_domain"]


def get_pack_version(pack_id: Optional[str] = None) -> str:
    """Return the pack version that produced a finding (R16-B1 §4).

    Stamped onto every opportunity instance alongside the pack id. Falls back to
    DEFAULT_PACK_VERSION for a pack that has not declared a "packVersion".
    """
    return get_pack(pack_id).get("packVersion", DEFAULT_PACK_VERSION)


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


def get_pack_config_path(pack_id: Optional[str] = None) -> Optional[str]:
    """Return the path to a pack's externalized config file, or None if it has none.

    MSP-B6 T1: the Cloud-Operations pack keeps its calibration values, thresholds,
    and terminology in an external JSON file (loaded via cloud_ops_config.py) so a
    config change alters behaviour with no code deploy (AC2).
    """
    return get_pack(pack_id).get("config_path")
def is_strs_benefits_pack(pack_id: Optional[str] = None) -> bool:
    """
    Returns True when the active pack is STRS Benefits Administration.
    Used in runner.py, scorer, and evidence_builder for pack routing.
    """
    return get_pack(pack_id)["domain"] == "strs_benefits"


def list_packs() -> List[str]:
    """Return all registered pack IDs."""
    return list(PACK_REGISTRY.keys())


def normalize_pack_ids(
    pack_ids: Optional[Any] = None,
) -> List[str]:
    """Normalise a multi-pack selection to an order-preserving, de-duplicated list.

    R191-P1 T1 groundwork: run configuration now accepts ``pack_ids: list[str]``
    in place of a singular ``pack_id``. This is the single normalisation primitive
    every config entry point (LaunchRequest, ComputeRequest, run record) routes
    through so the rules cannot drift between callers:

      * order-preserving — the caller's ordering is kept (the first id is the
        primary pack, which single-pack execution continues to run against, so a
        single-element list stays byte-identical to today);
      * de-duplicated — a repeated id keeps its first occurrence only;
      * empty/whitespace/non-string entries are dropped;
      * ``None`` (or a bare string, for caller convenience) is accepted.

    Unknown pack ids are intentionally NOT filtered here — validation/fallback of
    an individual id stays the job of ``get_pack()`` (which warns + falls back),
    exactly as for the singular path, so behaviour is unchanged for a single id.
    """
    if pack_ids is None:
        return []
    if isinstance(pack_ids, str):
        pack_ids = [pack_ids]

    seen: set[str] = set()
    normalized: List[str] = []
    for raw in pack_ids:
        if not isinstance(raw, str):
            continue
        pid = raw.strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        normalized.append(pid)
    return normalized


def is_ncino_pack(pack_id: Optional[str] = None) -> bool:
    """
    Convenience helper — replaces the temporary is_ncino_pack conditional.
    Returns True when the active pack is nCino domain.
    """
    return get_pack(pack_id)["domain"] == "ncino"


def is_sqlserver_opsignal_pack(pack_id: Optional[str] = None) -> bool:
    """Return True when the active pack is the SQL Server Operational Signal pack.

    Used in runner.py and scorer for pack routing.  Follows the identical
    pattern as is_ncino_pack() and is_strs_benefits_pack().
    """
    return get_pack(pack_id)["domain"] == "sqlserver_opsignal"


def is_github_engineering_pack(pack_id: Optional[str] = None) -> bool:
    """Return True when the active pack is the GitHub Engineering Signal pack.

    Used in runner.py and scorer for pack routing.  Follows the identical
    pattern as is_ncino_pack() and is_sqlserver_opsignal_pack().
    """
    return get_pack(pack_id)["domain"] == "github_engineering"


def is_enterprise_ops_pack(pack_id: Optional[str] = None) -> bool:
    """Return True when the active pack is the Enterprise Operations Intelligence pack.

    Used in runner.py and scorer for pack routing.  Follows the identical
    pattern as is_ncino_pack() and is_github_engineering_pack().
    """
    return get_pack(pack_id)["domain"] == "enterprise_ops"


def is_cloud_ops_pack(pack_id: Optional[str] = None) -> bool:
    """Return True when the active pack is the Cloud-Operations Discovery pack (MSP-B6).

    Used in runner.py and scorer for pack routing.  Follows the identical
    pattern as is_ncino_pack() and is_enterprise_ops_pack().
    """
    return get_pack(pack_id)["domain"] == "cloud_ops"


def is_security_ops_pack(pack_id: Optional[str] = None) -> bool:
    """Return True when the active pack is the Security-Operations Discovery pack (MSP-B12).

    Used in runner.py and scorer for pack routing.  Follows the identical
    pattern as is_ncino_pack() and is_cloud_ops_pack().
    """
    return get_pack(pack_id)["domain"] == "security_ops"
