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
# 2.0-C1 T3 (AT-828): config artifacts of PRIOR pack versions that remain runnable.
# See versions/README.md — the archive is what makes rollback honest rather than a
# version stamp that lies about which behaviour actually executed.
_VERSIONS_DIR = _PACKS_DIR / "versions"

PACK_REGISTRY: Dict[str, Dict[str, Any]] = {

    "service_cloud": {
        "packId":        "service_cloud",
        "packVersion":   "1.0.0",
        "packName":      "Service Cloud",
        "domain":        "service_cloud",
        "pack_domain":   "service_cloud",
        # 2.0-C1 T1 (AT-826): platform-capability range + required normalised
        # concepts. See COMPATIBILITY_KEY below and pack_compatibility.py.
        "compatibility": {
            "minPlatformVersion": "1.0.0",
            "maxPlatformVersion": None,
            "requiredConcepts":   ["case_workflow"],
            "optionalConcepts":   ["cross_system_link"],
        },
        # 2.0-C2 T1 (AT-831): certification metadata. Signed by CloudFulcrum — see
        # CERTIFICATION_KEY above and pack_certification.py.
        "certification": {
            "level":                          "certified",
            "certifyingEntity":               "CloudFulcrum",
            "reviewDate":                     "2026-07-31",
            "reviewedAgainstPlatformVersion": "2.0.0",
            "scope": {
                "summary": (
                    "Service Cloud detectors, evidence discipline, Salesforce "
                    "service terminology, and scorer calibration."
                ),
                "criteria": [
                    "declarative_manifest_review",
                    "evidence_discipline",
                    "terminology",
                    "calibration_sanity",
                ],
            },
            "signature": {
                "keyId":     "cloudfulcrum-pack-signing-2026",
                "algorithm": "ed25519",
                "value":     "Qfv6UmzZjcHillO69QwDAd/ii6IMzEug53F00N/fSQ+fAMwulKsSRSOBcSBTEYaiVohWe7otgQY9uZxpPI0pBg==",
            },
        },
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
        "compatibility": {
            "minPlatformVersion": "1.0.0",
            "maxPlatformVersion": None,
            "requiredConcepts":   ["loan_origination_workflow"],
            "optionalConcepts":   ["case_workflow", "cross_system_link"],
        },
        "certification": {
            "level":                          "certified",
            "certifyingEntity":               "CloudFulcrum",
            "reviewDate":                     "2026-07-31",
            "reviewedAgainstPlatformVersion": "2.0.0",
            "scope": {
                "summary": (
                    "nCino lending detectors, evidence discipline, banking "
                    "terminology, scorer calibration, and the no-automated-credit"
                    "-decision compliance guardrail."
                ),
                "criteria": [
                    "declarative_manifest_review",
                    "evidence_discipline",
                    "terminology",
                    "calibration_sanity",
                    "compliance_guardrails",
                ],
            },
            "signature": {
                "keyId":     "cloudfulcrum-pack-signing-2026",
                "algorithm": "ed25519",
                "value":     "ov5cfGllurB91X864BzTM0URZeSKzgUdZsmCDI1qrxtCAUKTcuPtP0gxzud0E+y5TgiNAhLmUaL/1zqIInfSDA==",
            },
        },
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

    # 2.0-D1 T1 — Financial Services Cloud pack registration.
    #
    # Registered on the EXISTING pack framework: identity, versioning, and
    # terminology, following the shape every shipped pack already uses. The
    # closest analogue is the "ncino" entry directly above — same underlying
    # Salesforce connector, same BFSI domain family, same compliance posture —
    # so this entry is a copy of that shape, not of "service_cloud".
    #
    # 2.0-D1 T2 populated `detectors` with the five FSC detectors — 80% reuse, as
    # the story intended: four are existing detector shapes recomposed for FSC
    # (repetition -> servicing recurrence, handoff_friction -> referral friction,
    # approval_delay+approval_bottleneck -> approval/review cycle,
    # cross_system_echo -> cross-object rework) and only service-queue ageing is
    # new, itself modelled on cloud_ops_queue_ageing's per-queue-own-baseline rule.
    # Each module documents which detector it was composed from and what did not
    # transfer.
    #
    # This pack is deliberately NOT yet listed in any industry's `pack_hints`
    # (industry_registry.py) nor bound to a Stack Builder template — that is the
    # story's registry-honesty rule (the FSC entry appears on a selectable
    # surface only when the pack actually ships) and is T4's scope.
    "financial_services_cloud": {
        "packId":        "financial_services_cloud",
        # R16-B1 §4: stamped onto every opportunity this pack produces. Bumping
        # this is a LIVE obligation, not a one-time field — bump it in the same PR
        # as ANY change to this pack's detectors, scorer, or corroboration rules.
        # 2.0-A2 outcome tracking reads this version to flag a measurement taken
        # across a pack-version boundary as a confounder, so a version that never
        # moves silently breaks that story. A pinned fingerprint in
        # test_financial_services_cloud_pack_registration.py fails the build if the
        # pack surface changes without an intentional bump.
        #
        # 1.0.0 -> 1.1.0 by T2: the five detectors + their externalised thresholds
        # are a behaviour change, and this is the intentional bump that guard
        # exists to force. 1.1.0 -> 1.2.0 by T3: the FSC scorer calibration
        # (financial_services_cloud_scorer.py's _FSC_SCORES + the three config
        # dimension weights) changes how findings are scored and ranked, which is
        # a pack-logic change by the same rule. Keep this in lockstep with
        # packVersion in financial_services_cloud_pack_config.json (a test pins
        # the two together).
        "packVersion":   "1.2.0",
        "packName":      "Financial Services Cloud",
        "domain":        "financial_services_cloud",
        "pack_domain":   "financial_services_cloud",
        "detectors": [
            "discovery.detectors.fsc_servicing_request_recurrence",
            "discovery.detectors.fsc_referral_handoff_friction",
            "discovery.detectors.fsc_approval_review_cycle",
            "discovery.detectors.fsc_service_queue_ageing",
            "discovery.detectors.fsc_cross_object_rework",
        ],
        # T2: firing thresholds, the AC5 aggregation floor, and terminology load
        # from this external file (via financial_services_cloud_config.py) rather
        # than from inline detector constants, so replacing a PROVISIONAL number
        # is a config edit. T3 filled `calibration` with the three dimension
        # weights the FSC scorer ranks by (the per-detector base scores live in
        # financial_services_cloud_scorer.py's _FSC_SCORES, with inline provenance).
        "config_path":   str(_PACKS_DIR / "financial_services_cloud_pack_config.json"),
        # Terminology is DATA, not code: every user-visible FSC string
        # (households, relationship groups, financial accounts, service
        # processes, referrals) resolves through this label file, so the pack
        # code stays terminology-neutral.
        "ui_labels_path": str(_PACKS_DIR / "financial_services_cloud_ui_labels.json"),
        "llm_context": (
            "Salesforce Financial Services Cloud (FSC) client-servicing analysis "
            "for retail banking and wealth management. "
            "Speak FSC language: households, relationship groups, financial "
            "accounts, service processes, referrals, action plans, and client "
            "interactions. "
            "Focus on servicing-request recurrence, referral and handoff friction "
            "between teams, approval and review cycle time, ageing in service "
            "queues, and cross-object rework across household, financial account, "
            "and service process records. "
            "Use banking and wealth-servicing operations language — not "
            "Salesforce admin language. "
            "Reference households, relationship groups, teams, queues, and "
            "service processes only — never an individual client, adviser, or "
            "banker. "
            "Regulatory compliance (FCA, SEC, OCC) is the highest-weight signal "
            "category; approval audit trails and review documentation are "
            "priority friction patterns. "
            "IMPORTANT: never suggest automated credit or compliance decisions. "
            "All credit, suitability, and compliance decisions require human "
            "approval."
        ),
    },

    "strs_benefits": {
        "packId":        "strs_benefits",
        "packVersion":   "1.0.0",
        "packName":      "STRS Benefits Administration",
        "domain":        "strs_benefits",
        "pack_domain":   "strs_benefits",
        "compatibility": {
            "minPlatformVersion": "1.0.0",
            "maxPlatformVersion": None,
            "requiredConcepts":   ["benefit_administration_workflow"],
            # STRS corroborates against Jira/ServiceNow when connected.
            "optionalConcepts":   ["cross_system_link"],
        },
        "certification": {
            "level":                          "certified",
            "certifyingEntity":               "CloudFulcrum",
            "reviewDate":                     "2026-07-31",
            "reviewedAgainstPlatformVersion": "2.0.0",
            "scope": {
                "summary": (
                    "STRS benefit-administration detectors, evidence discipline, "
                    "member-services terminology, scorer calibration, and the "
                    "no-automated-benefit-decision compliance guardrail."
                ),
                "criteria": [
                    "declarative_manifest_review",
                    "evidence_discipline",
                    "terminology",
                    "calibration_sanity",
                    "compliance_guardrails",
                ],
            },
            "signature": {
                "keyId":     "cloudfulcrum-pack-signing-2026",
                "algorithm": "ed25519",
                "value":     "msWGygHbpWAhbsLU7z6Wt6QiGIHxxAxSkmf5Gs2+OAah5/UZn5oGsNLPpQxzybLgZ7iKigsnlsL7QqkhtAVpAg==",
            },
        },
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
        "compatibility": {
            "minPlatformVersion": "1.0.0",
            "maxPlatformVersion": None,
            "requiredConcepts":   ["db_operational_signal"],
            "optionalConcepts":   ["cross_system_link"],
        },
        "certification": {
            "level":                          "certified",
            "certifyingEntity":               "CloudFulcrum",
            "reviewDate":                     "2026-07-31",
            "reviewedAgainstPlatformVersion": "2.0.0",
            "scope": {
                "summary": (
                    "SQL Server operational-signal detectors, evidence discipline, "
                    "IT-operations terminology, and scorer calibration."
                ),
                "criteria": [
                    "declarative_manifest_review",
                    "evidence_discipline",
                    "terminology",
                    "calibration_sanity",
                ],
            },
            "signature": {
                "keyId":     "cloudfulcrum-pack-signing-2026",
                "algorithm": "ed25519",
                "value":     "EMx21tys0xShGqHjCnHKNTGpALIna5HprJnHRwCdp2GgjbiDfowWaxTovvm7lDHeHvNv0qa1mZoD/y/3fuM7BQ==",
            },
        },
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
        # R191-R1: bumped 1.0.0 → 1.0.1 alongside the industry_registry.py change
        # that activates this pack as a default for the technology industry, so
        # pack governance can tell technology-industry runs before/after the
        # activation apart by version stamp.
        "packVersion":   "1.0.1",
        "packName":      "GitHub Engineering Signals",
        "domain":        "github_engineering",
        "pack_domain":   "github_engineering",
        "compatibility": {
            "minPlatformVersion": "1.0.0",
            "maxPlatformVersion": None,
            "requiredConcepts":   ["code_activity_signal"],
            # Jira corroboration elevates PR-bottleneck confidence when present.
            "optionalConcepts":   ["cross_system_link"],
        },
        "certification": {
            "level":                          "certified",
            "certifyingEntity":               "CloudFulcrum",
            "reviewDate":                     "2026-07-31",
            "reviewedAgainstPlatformVersion": "2.0.0",
            "scope": {
                "summary": (
                    "GitHub engineering-signal detectors, evidence discipline, "
                    "engineering-operations terminology, scorer calibration, and "
                    "the no-automated-merge compliance guardrail."
                ),
                "criteria": [
                    "declarative_manifest_review",
                    "evidence_discipline",
                    "terminology",
                    "calibration_sanity",
                    "compliance_guardrails",
                ],
            },
            "signature": {
                "keyId":     "cloudfulcrum-pack-signing-2026",
                "algorithm": "ed25519",
                "value":     "dZhxwl9WMMFeUJLIt38ZNs8TgeaYLPJs7FFm2T7gRgNmwoamd2PQkG18NVu0fLXmNdu/KegpAAzqB75rplVODQ==",
            },
        },
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
        # Findings emerge from the GAP between ServiceNow incidents and Jira
        # issues, so the cross-system link is a HARD requirement here, not an
        # optional corroborator: without it there is nothing for this pack to see.
        "compatibility": {
            "minPlatformVersion": "1.0.0",
            "maxPlatformVersion": None,
            "requiredConcepts":   ["incident_workflow", "cross_system_link"],
            "optionalConcepts":   [],
        },
        "certification": {
            "level":                          "certified",
            "certifyingEntity":               "CloudFulcrum",
            "reviewDate":                     "2026-07-31",
            "reviewedAgainstPlatformVersion": "2.0.0",
            "scope": {
                "summary": (
                    "Cross-system enterprise-operations detectors, evidence "
                    "discipline, operations-leadership terminology, and scorer "
                    "calibration."
                ),
                "criteria": [
                    "declarative_manifest_review",
                    "evidence_discipline",
                    "terminology",
                    "calibration_sanity",
                ],
            },
            "signature": {
                "keyId":     "cloudfulcrum-pack-signing-2026",
                "algorithm": "ed25519",
                "value":     "tBhUu0oy7tJeCMBDn8Ummb5Bmb7gDiJoTCimslXZ1xnfovNbGQb96aFjXVx7zOOW7EIvAPzs+Q/O/7UT0McSAQ==",
            },
        },
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
        # 1.2.1 — 2.0-B1 T7: cloud_ops_finding._filter_correlation_windows now
        # fails CLOSED on a non-boolean within_window flag (patch: the pack's
        # corroboration-part logic changed, but no real producer's output does —
        # every producer already emits a bool).
        "packVersion":   "1.2.1",
        "packName":      "Cloud Operations",
        "domain":        "cloud_ops",
        "pack_domain":   "cloud_ops",
        # 2.0-C1 T1 (AT-826): this pack's detectors read the MSP-B4 deterministic
        # signatures and group-routing history plus the MSP-B0/B7 operational-event
        # stream, so those are HARD requirements and the range floor is the MSP
        # release. MSP-B3 (CMDB) and MSP-B5 (runbooks) are declared OPTIONAL
        # because the pack degrades honestly without them by design — the
        # recurrence stays unlocated (MSP-B4 AC5) and the runbook leg downgrades to
        # "runbook match unavailable" (MSP-B6 T6). Declaring them as required would
        # misreport a graceful degradation as an incompatibility.
        "compatibility": {
            "minPlatformVersion": "1.9.0",
            "maxPlatformVersion": None,
            "requiredConcepts": [
                "incident_workflow",
                "resolution_signature",
                "incident_identity_signature",
                "assignment_group_routing",
                "operational_event",
            ],
            "optionalConcepts": ["cmdb_dependency", "runbook_match"],
        },
        "certification": {
            "level":                          "certified",
            "certifyingEntity":               "CloudFulcrum",
            "reviewDate":                     "2026-07-31",
            "reviewedAgainstPlatformVersion": "2.0.0",
            "scope": {
                "summary": (
                    "Cloud-operations detectors, the MSP-B6 four-part finding "
                    "contract and causal gate, NOC terminology, and the "
                    "config-driven ops-impact scorer calibration."
                ),
                "criteria": [
                    "declarative_manifest_review",
                    "evidence_discipline",
                    "terminology",
                    "calibration_sanity",
                    "aggregation_floor",
                ],
            },
            "signature": {
                "keyId":     "cloudfulcrum-pack-signing-2026",
                "algorithm": "ed25519",
                "value":     "v8EJRihCPB8BuJOj+sRGl0zVPifpVUwIJgUpprWg0UEyzzRJ8pN0wkMkZi6S7yLdrUN8UTt/4Z6jhE4K8zKAAQ==",
            },
        },
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
        # 2.0-C1 T3 (AT-828): PRIOR versions that remain runnable, newest first.
        # Each entry carries the real behaviour of that version — its archived
        # config artifact AND its detector list — so a run pinned to it executes
        # 1.1.0 rather than being stamped 1.1.0 while running 1.2.0. 1.1.0 is
        # MSP-B6 T4's state: the five Section-2 detectors, before MSP-B5 wiring
        # added the documentation-gap detector. 1.0.0 is deliberately NOT offered
        # (it was the T1 scaffold with ZERO detectors — see versions/README.md).
        "versionHistory": [
            {
                "version": "1.1.0",
                "configPath": str(
                    _VERSIONS_DIR / "cloud_ops_pack_config.v1.1.0.json"
                ),
                "detectors": [
                    "discovery.detectors.cloud_ops_recurring_resolution_loop",
                    "discovery.detectors.cloud_ops_alert_triage_toil",
                    "discovery.detectors.cloud_ops_reassignment_ping_pong",
                    "discovery.detectors.cloud_ops_queue_ageing",
                    "discovery.detectors.cloud_ops_shared_ci_hotspot",
                ],
                "note": "MSP-B6 T4 (AT-739) — before the MSP-B5 documentation-gap detector",
            },
        ],
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
        # 2.0-C1 T1 (AT-826): consumes MSP-B11's SecOps workflow signal, so both
        # SecOps concepts are hard requirements and the floor is the MSP release.
        # MSP-B3's CMDB is optional — the shared-infra concentration detector
        # narrows with it and still emits without it.
        "compatibility": {
            "minPlatformVersion": "1.9.0",
            "maxPlatformVersion": None,
            "requiredConcepts": [
                "security_incident_workflow",
                "vulnerability_workflow",
            ],
            "optionalConcepts": ["cmdb_dependency", "incident_workflow"],
        },
        # Second sibling of the Cloud-Operations pack on the same template model.
        # MSP-B12 T2 (Section 1) detectors — consume only MSP-B11's SecOps workflow
        # signal (sn_data['secops'] / ['vulnerability_response']) + B3's
        # sn_data['cmdb']. Invoked via the runner's uniform pack-dispatch branch.
        "certification": {
            "level":                          "certified",
            "certifyingEntity":               "CloudFulcrum",
            "reviewDate":                     "2026-07-31",
            "reviewedAgainstPlatformVersion": "2.0.0",
            "scope": {
                "summary": (
                    "Security-operations detectors, evidence discipline, SecOps "
                    "terminology, scorer calibration, and the MSP-B11 aggregation "
                    "floor that forbids host x vulnerability enumeration."
                ),
                "criteria": [
                    "declarative_manifest_review",
                    "evidence_discipline",
                    "terminology",
                    "calibration_sanity",
                    "aggregation_floor",
                ],
            },
            "signature": {
                "keyId":     "cloudfulcrum-pack-signing-2026",
                "algorithm": "ed25519",
                "value":     "PyXpslUI87jVdYfJ7MaQ6Fr+U0vWAse5X7bCwbKWeVdEUFxRQH5MSYNwzqMvm0BaEMX/5/f4x2RfkaAlbV4hAg==",
            },
        },
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
        # 2.0-C1 T3 (AT-828): prior runnable versions, newest first. 1.1.0 is
        # MSP-B12 T2's state — the five Section-1 detectors with that release's
        # calibration. 1.0.0 (the T1 scaffold, zero detectors) is deliberately not
        # offered as a rollback target; see versions/README.md.
        "versionHistory": [
            {
                "version": "1.1.0",
                "configPath": str(
                    _VERSIONS_DIR / "security_ops_pack_config.v1.1.0.json"
                ),
                "detectors": [
                    "discovery.detectors.security_ops_remediation_recurrence",
                    "discovery.detectors.security_ops_security_it_pingpong",
                    "discovery.detectors.security_ops_sla_deferral_ageing",
                    "discovery.detectors.security_ops_shared_infra_concentration",
                    "discovery.detectors.security_ops_sir_triage_toil",
                ],
                "note": "MSP-B12 T2 — the five Section-1 detectors",
            },
        ],
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

# 2.0-C1 T1 (AT-826): the registry key each pack declares its compatibility under.
# Shape (all four keys optional; see platform_capabilities.py for the vocabulary):
#
#   "compatibility": {
#       "minPlatformVersion": "1.9.0",     # inclusive floor;  None ⇒ no floor
#       "maxPlatformVersion": None,        # inclusive ceiling; None ⇒ open-ended
#       "requiredConcepts":   [...],       # gating — an unmet concept refuses activation
#       "optionalConcepts":   [...],       # advisory — the pack degrades honestly without these
#   }
#
# The gate lives in pack_compatibility.py, not here: this module stays the
# declaration surface (like packVersion) so a pack's requirements are read in the
# same place as the rest of its config.
COMPATIBILITY_KEY = "compatibility"

# 2.0-C1 T3 (AT-828): the registry key listing PRIOR pack versions that remain
# runnable, newest first. Each entry declares the real behaviour of that version:
#
#   {
#       "version":    "1.1.0",              # must differ from the current packVersion
#       "configPath": ".../pack_config.v1.1.0.json",   # archived artifact (see versions/)
#       "detectors":  [...],                # that version's detector list
#       "note":       "free-text provenance",
#   }
#
# A pack with NO versionHistory has no rollback target and rollback is refused with
# that reason named — the platform declines to pretend it can serve behaviour it no
# longer has. Only config-driven packs (cloud_ops, security_ops) can currently
# declare history; a code-only pack would have to externalise its calibration first.
VERSION_HISTORY_KEY = "versionHistory"

# 2.0-C2 T1 (AT-831): the registry key each pack declares its certification under.
# Shape (see pack_certification.py for the vocabulary and the verification rules):
#
#   "certification": {
#       "level":                          "certified" | "partner" | "community",
#       "certifyingEntity":               "CloudFulcrum",
#       "reviewDate":                     "2026-07-31",       # ISO date of the review
#       "reviewedAgainstPlatformVersion": "2.0.0",            # platform capability version
#       "scope": {
#           "summary":  "what the review covered",
#           "criteria": ["evidence_discipline", ...],         # AT-832's checklist ids
#       },
#       "signature": {                    # required for certified/partner ONLY
#           "keyId":     "cloudfulcrum-pack-signing-2026",
#           "algorithm": "ed25519",
#           "value":     "<base64 signature over the canonical payload>",
#       },
#   }
#
# The signature is what stops the label being self-applied: everything above except
# the signature block itself is inside the signed payload, so no field can be edited
# after issuance without invalidating it. A certified/partner claim that does not
# verify is reported as community, with the reason named.
#
# As with COMPATIBILITY_KEY, the gate lives elsewhere (pack_certification.py) and this
# module stays the declaration surface.
CERTIFICATION_KEY = "certification"

# Fallback for a pack that declares no certification block. Community is the honest
# default: an undeclared pack has not been reviewed by anybody, and community is
# precisely the label for that — so the absence of a declaration is never an error.
DEFAULT_PACK_CERTIFICATION: Dict[str, Any] = {
    "level": "community",
    "certifyingEntity": "",
    "reviewDate": "",
    "reviewedAgainstPlatformVersion": "",
    "scope": {"summary": "", "criteria": []},
    "signature": {"keyId": "", "algorithm": "", "value": ""},
}

# 2.0-C4 T1 (AT-842): the registry key a pack declares its DEPRECATION under.
# Shape (see pack_deprecation.py for the phases and the evaluation rules):
#
#   "deprecation": {
#       "status":           "deprecated",          # "active" (default) | "deprecated"
#       "versions":         ["1.1.0"],             # [] / omitted ⇒ EVERY version
#       "reason":           "Superseded by ...",   # required for a real notice
#       "deprecatedOn":     "2026-08-01",          # ISO date the notice starts
#       "gracePeriodDays":  90,                    # derives graceEndsOn from deprecatedOn
#       "graceEndsOn":      "2026-10-30",          # authoritative end date, if known
#       "replacement": {                           # optional — the migration path
#           "packId":     "cloud_ops",
#           "minVersion": "1.2.0",
#           "notes":      "free-text migration guidance",
#       },
#   }
#
# Declare EITHER gracePeriodDays OR graceEndsOn; declaring neither is a legitimate
# "deprecated, no removal date announced yet" state that surfaces the notice and
# never auto-disables the pack.
#
# As with COMPATIBILITY_KEY and CERTIFICATION_KEY, the rules live elsewhere
# (pack_deprecation.py) and this module stays the declaration surface. Unlike
# certification there is no signature: a deprecation is the registry shipper stating
# that its OWN pack is superseded, so there is no third-party claim to protect.
DEPRECATION_KEY = "deprecation"

# Fallback for a pack that declares no deprecation block — not deprecated. The
# overwhelmingly common case, so the absence of a declaration is never an error.
DEFAULT_PACK_DEPRECATION: Dict[str, Any] = {
    # Empty means UNDECLARED, which pack_deprecation resolves to "active" — kept
    # distinct from an explicit "active" so it can tell a pack that says nothing
    # from one that says "not deprecated", and so a block that carries a reason and
    # a date but forgot its status is read as the notice it plainly is.
    "status": "",
    "versions": [],
    "reason": "",
    "deprecatedOn": "",
    "gracePeriodDays": None,
    "graceEndsOn": "",
    "replacement": {"packId": "", "minVersion": "", "notes": ""},
}

# Fallback for a pack that has not declared a "compatibility" block. Deliberately
# PERMISSIVE (no bounds, no required concepts) so an undeclared pack behaves
# exactly as it did before AT-826 — the declaration is enforced by a structural
# test over PACK_REGISTRY, not by silently refusing to run an undeclared pack.
DEFAULT_PACK_COMPATIBILITY: Dict[str, Any] = {
    "minPlatformVersion": None,
    "maxPlatformVersion": None,
    "requiredConcepts": [],
    "optionalConcepts": [],
}


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


def get_pack_compatibility_declaration(
    pack_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a pack's declared compatibility block (2.0-C1 T1 / AT-826).

    Always returns a complete block: a pack that declares nothing (or declares a
    partial block) is filled from ``DEFAULT_PACK_COMPATIBILITY``, so callers never
    need to handle a missing key. Concept lists are normalised to de-duplicated,
    order-preserving lists of non-empty strings.

    Resolution follows ``get_pack()`` — an unknown pack id reads the DEFAULT
    pack's declaration, exactly as it reads the default pack's detectors, so an
    unknown id is never refused on compatibility grounds it does not have.
    """
    declared = get_pack(pack_id).get(COMPATIBILITY_KEY) or {}
    if not isinstance(declared, dict):
        declared = {}

    def _concepts(key: str) -> List[str]:
        raw = declared.get(key, DEFAULT_PACK_COMPATIBILITY[key])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        seen: set[str] = set()
        out: List[str] = []
        for entry in raw:
            if not isinstance(entry, str):
                continue
            concept = entry.strip()
            if not concept or concept in seen:
                continue
            seen.add(concept)
            out.append(concept)
        return out

    def _bound(key: str) -> Optional[str]:
        raw = declared.get(key, DEFAULT_PACK_COMPATIBILITY[key])
        if not isinstance(raw, str):
            return None
        bound = raw.strip()
        return bound or None

    return {
        "minPlatformVersion": _bound("minPlatformVersion"),
        "maxPlatformVersion": _bound("maxPlatformVersion"),
        "requiredConcepts": _concepts("requiredConcepts"),
        "optionalConcepts": _concepts("optionalConcepts"),
    }


def get_pack_certification_declaration(
    pack_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a pack's declared certification block (2.0-C2 T1 / AT-831).

    Always returns a complete, normalised block: a pack that declares nothing (or a
    partial block) is filled from ``DEFAULT_PACK_CERTIFICATION``, so callers never
    handle a missing key — an undeclared pack reads as ``community``, which is the
    honest label for "nobody has vouched for this".

    Normalisation is deliberately conservative and lossless-in-shape, because the
    result is the input to the SIGNED payload: strings are stripped, the level is
    lower-cased, and ``scope.criteria`` is de-duplicated order-preservingly. Nothing
    is invented or defaulted into a signed field — an absent value stays empty, so a
    signature can never cover something the pack did not actually declare.

    Resolution follows ``get_pack()``, so an unknown pack id reads the DEFAULT pack's
    certification, exactly as it reads its detectors.
    """
    declared = get_pack(pack_id).get(CERTIFICATION_KEY) or {}
    if not isinstance(declared, dict):
        declared = {}

    def _string(source: Dict[str, Any], key: str) -> str:
        raw = source.get(key)
        return raw.strip() if isinstance(raw, str) else ""

    scope_raw = declared.get("scope")
    if not isinstance(scope_raw, dict):
        scope_raw = {}
    criteria_raw = scope_raw.get("criteria")
    if isinstance(criteria_raw, str):
        criteria_raw = [criteria_raw]
    if not isinstance(criteria_raw, (list, tuple)):
        criteria_raw = []
    seen: set[str] = set()
    criteria: List[str] = []
    for entry in criteria_raw:
        if not isinstance(entry, str):
            continue
        item = entry.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        criteria.append(item)

    signature_raw = declared.get("signature")
    if not isinstance(signature_raw, dict):
        signature_raw = {}

    return {
        "level": (
            _string(declared, "level").lower()
            or DEFAULT_PACK_CERTIFICATION["level"]
        ),
        "certifyingEntity": _string(declared, "certifyingEntity"),
        "reviewDate": _string(declared, "reviewDate"),
        "reviewedAgainstPlatformVersion": _string(
            declared, "reviewedAgainstPlatformVersion"
        ),
        "scope": {
            "summary": _string(scope_raw, "summary"),
            "criteria": criteria,
        },
        "signature": {
            "keyId": _string(signature_raw, "keyId"),
            "algorithm": _string(signature_raw, "algorithm").lower(),
            "value": _string(signature_raw, "value"),
        },
    }


def get_pack_deprecation_declaration(
    pack_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a pack's declared deprecation block (2.0-C4 T1 / AT-842).

    Always returns a complete block: a pack that declares nothing (or a partial
    block) is filled from ``DEFAULT_PACK_DEPRECATION``, so callers never handle a
    missing key. An undeclared ``status`` is reported as EMPTY rather than
    ``"active"``, so ``pack_deprecation`` can tell "said nothing" (resolve to active,
    or infer a notice from the rest of the block) from an explicit "not deprecated".

    Normalisation only cleans shapes, it never repairs meaning: strings are stripped,
    the status is lower-cased, ``versions`` is de-duplicated order-preservingly, and
    ``gracePeriodDays`` is passed through UNCHANGED when it is not a whole number so
    that ``pack_deprecation`` can name it as a defect rather than have it silently
    disappear. Inventing a missing reason or grace date here would hide exactly the
    declaration mistakes the structural tests exist to catch.

    Resolution follows ``get_pack()``, so an unknown pack id reads the DEFAULT pack's
    deprecation, exactly as it reads its detectors.
    """
    declared = get_pack(pack_id).get(DEPRECATION_KEY) or {}
    if not isinstance(declared, dict):
        declared = {}

    def _string(source: Dict[str, Any], key: str) -> str:
        raw = source.get(key)
        return raw.strip() if isinstance(raw, str) else ""

    versions_raw = declared.get("versions")
    if isinstance(versions_raw, str):
        versions_raw = [versions_raw]
    if not isinstance(versions_raw, (list, tuple)):
        versions_raw = []
    seen: set[str] = set()
    versions: List[str] = []
    for entry in versions_raw:
        if not isinstance(entry, str):
            continue
        version = entry.strip()
        if not version or version in seen:
            continue
        seen.add(version)
        versions.append(version)

    replacement_raw = declared.get("replacement")
    if not isinstance(replacement_raw, dict):
        replacement_raw = {}

    return {
        # Empty ⇒ undeclared. pack_deprecation resolves that to "active", or infers
        # "deprecated" when the rest of the block is populated.
        "status": _string(declared, "status").lower(),
        "versions": versions,
        "reason": _string(declared, "reason"),
        "deprecatedOn": _string(declared, "deprecatedOn"),
        # Passed through as declared — a non-integer is a defect to be NAMED by
        # pack_deprecation, not something to coerce away here.
        "gracePeriodDays": declared.get("gracePeriodDays"),
        "graceEndsOn": _string(declared, "graceEndsOn"),
        "replacement": {
            "packId": _string(replacement_raw, "packId"),
            "minVersion": _string(replacement_raw, "minVersion"),
            "notes": _string(replacement_raw, "notes"),
        },
    }


def get_pack_version_history(pack_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Prior runnable versions of a pack, newest first (2.0-C1 T3 / AT-828).

    Each entry is normalised to ``{version, configPath, detectors, note}``. Entries
    without a usable ``version`` are dropped, and an entry whose version equals the
    pack's CURRENT ``packVersion`` is dropped too — the current version is served
    from the pack's own config, never from the archive, so listing it would create
    two sources of truth for one version.

    Returns ``[]`` for a pack that declares no history (no rollback target).
    """
    pack = get_pack(pack_id)
    declared = pack.get(VERSION_HISTORY_KEY) or []
    if not isinstance(declared, (list, tuple)):
        return []
    current = str(pack.get("packVersion", DEFAULT_PACK_VERSION))

    history: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        version = str(entry.get("version") or "").strip()
        if not version or version == current or version in seen:
            continue
        seen.add(version)
        detectors = entry.get("detectors")
        history.append(
            {
                "version": version,
                "configPath": (
                    str(entry["configPath"]).strip()
                    if isinstance(entry.get("configPath"), str)
                    and str(entry["configPath"]).strip()
                    else None
                ),
                "detectors": (
                    [str(d) for d in detectors if str(d).strip()]
                    if isinstance(detectors, (list, tuple))
                    else None
                ),
                "note": entry.get("note"),
            }
        )
    return history


def get_pack_version_entry(
    pack_id: Optional[str], version: str
) -> Optional[Dict[str, Any]]:
    """The archived history entry for one prior version, or ``None`` if not archived."""
    wanted = str(version or "").strip()
    if not wanted:
        return None
    for entry in get_pack_version_history(pack_id):
        if entry["version"] == wanted:
            return entry
    return None


def get_rollbackable_versions(pack_id: Optional[str] = None) -> List[str]:
    """Version strings this pack can be rolled back to, newest first."""
    return [entry["version"] for entry in get_pack_version_history(pack_id)]


def resolve_pack_at_version(
    pack_id: Optional[str], version: Optional[str] = None
) -> Dict[str, Any]:
    """The pack config AS IT BEHAVES at ``version`` (2.0-C1 T3 / AT-828).

    ``version`` ``None`` (or equal to the current ``packVersion``) returns the
    current registry config unchanged, so an un-pinned pack is byte-identical to
    before rollback existed.

    For a pinned PRIOR version, returns a COPY of the pack with that version's real
    behaviour substituted — ``packVersion``, ``detectors``, and ``config_path`` —
    plus ``pinnedVersion`` so a consumer can tell a pinned resolution from a normal
    one. The registry itself is never mutated.

    Raises :class:`PackVersionUnavailable` for a version with no archived entry:
    serving the CURRENT behaviour under an older stamp would be a lie, and silently
    ignoring the pin would make rollback untrustworthy.
    """
    pack = get_pack(pack_id)
    current = str(pack.get("packVersion", DEFAULT_PACK_VERSION))
    wanted = str(version or "").strip()
    if not wanted or wanted == current:
        return pack

    entry = get_pack_version_entry(pack_id, wanted)
    if entry is None:
        raise PackVersionUnavailable(
            pack_id=pack["packId"],
            version=wanted,
            available=get_rollbackable_versions(pack_id),
            current=current,
        )

    resolved = dict(pack)
    resolved["packVersion"] = entry["version"]
    resolved["pinnedVersion"] = entry["version"]
    if entry["detectors"] is not None:
        resolved["detectors"] = list(entry["detectors"])
    if entry["configPath"] is not None:
        resolved["config_path"] = entry["configPath"]
    return resolved


class PackVersionUnavailable(LookupError):
    """A requested pack version has no archived artifact, so it cannot be served.

    ``str(exc)`` names the pack, the requested version, and the versions that ARE
    available, so a refusal is actionable.
    """

    def __init__(
        self,
        *,
        pack_id: str,
        version: str,
        available: List[str],
        current: str,
    ) -> None:
        self.pack_id = pack_id
        self.version = version
        self.available = list(available)
        self.current = current
        if self.available:
            options = (
                f"Versions available for rollback: {', '.join(self.available)} "
                f"(current: {current})."
            )
        else:
            options = (
                f"This pack has no archived prior versions, so it cannot be rolled "
                f"back (current: {current})."
            )
        super().__init__(
            f"Pack '{pack_id}' version {version!r} is not available to run. {options}"
        )


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


def is_financial_services_cloud_pack(pack_id: Optional[str] = None) -> bool:
    """Return True when the active pack is the Financial Services Cloud pack (2.0-D1).

    Used in runner.py and scorer for pack routing.  Follows the identical
    pattern as is_ncino_pack() and is_security_ops_pack().
    """
    return get_pack(pack_id)["domain"] == "financial_services_cloud"


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
