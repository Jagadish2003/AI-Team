"""
backend/discovery/packs/corroboration_rules.py

ENT-2 — Cross-System Confidence Elevation — Corroboration Rule Registry (T2).

This module defines the corroboration rules as DATA, separate from the engine
logic in backend/app/corroboration_engine.py. The goal is auditability: a lead,
reviewer, or business stakeholder can read this file and understand exactly
which corroboration rules exist, what they mean, and what confidence effect each
is permitted to have — WITHOUT reading the engine source.

This mirrors the configuration-driven approach already used by
backend/discovery/packs/pack_config.py (rules/behaviour defined centrally as
data, not buried in conditional logic).

Source of truth
---------------
The rule table here is the single source of truth for corroboration behaviour.
Per the ENT-2 spec Section 1: "If a rule is not in this table, it does not
happen. Adding a new rule requires updating this document and the
corroboration_rules.py file simultaneously."

Adding a new rule = add an entry here + add a check function in
corroboration_engine.py + add a contract test. No other changes.

THE SLACK CEILING (do not change without sign-off)
--------------------------------------------------
COR-05 is the Slack rule. Slack escalation pattern ALONE never elevates
confidence to HIGH. Slack is a *supporting* signal only — it elevates confidence
only when combined with a primary system corroborator (COR-06). The confidence
ceiling for Slack-only corroboration is MEDIUM. Under no circumstances should a
future rule elevate Slack-only to HIGH. This is a hardcoded enterprise
principle, represented explicitly in the COR-05 entry below
(elevation_target = "MEDIUM", elevates = False).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Confidence levels (shared vocabulary with the scoring framework)
# ─────────────────────────────────────────────────────────────────────────────

CONFIDENCE_LOW = "LOW"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_HIGH = "HIGH"

#: Shared freshness window for all time-sensitive corroboration rules.
#: Kept in the rule registry so reviewers can audit the window beside COR-01
#: through COR-08 instead of hunting in engine code.
CORROBORATION_WINDOW_DAYS = 30

#: Ordering used to compare / never-downgrade confidence levels.
CONFIDENCE_ORDER: Dict[str, int] = {
    CONFIDENCE_LOW: 0,
    CONFIDENCE_MEDIUM: 1,
    CONFIDENCE_HIGH: 2,
}


# ─────────────────────────────────────────────────────────────────────────────
# Rule definition dataclass — pure data, no behaviour
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CorroborationRule:
    """A single corroboration rule, defined as auditable data.

    Attributes
    ----------
    rule_id:
        Stable identifier, e.g. "COR-01". Referenced in CorroborationResult.rule_ids
        and OppEnrichment.corroboration_rule_ids so the elevation is auditable
        without reading engine code.
    primary_signal:
        Human description of the primary detector/system that must fire first.
    corroborating_signal:
        Human description of the supporting signal from another system.
    elevation_target:
        The confidence level this rule elevates TO when it fires
        ("HIGH" for elevating rules, "MEDIUM" for supporting-only rules).
    elevates:
        True if the rule raises confidence. False for supporting-only rules
        (COR-05 Slack-only, COR-08 single-source) which never raise confidence.
    source_label:
        The human label added to corroboration_sources when this rule fires
        (e.g. "ServiceNow", "Jira", "Slack (supporting only)"). None when the
        rule contributes no source chip (COR-03 is derived; COR-08 single-source).
    description:
        Plain-English explanation of the rule for auditors.
    """

    rule_id: str
    primary_signal: str
    corroborating_signal: str
    elevation_target: str
    elevates: bool
    source_label: Optional[str]
    description: str


# ─────────────────────────────────────────────────────────────────────────────
# THE RULE REGISTRY — ENT-2 Section 1a, verbatim semantics
# ─────────────────────────────────────────────────────────────────────────────

CORROBORATION_RULES: Dict[str, CorroborationRule] = {
    "COR-01": CorroborationRule(
        rule_id="COR-01",
        primary_signal="Any nCino detector fires (Salesforce)",
        corroborating_signal=(
            "ServiceNow shows open incidents assigned to the same team within 30 days"
        ),
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label="ServiceNow",
        description="Corroborated by ServiceNow incidents. MEDIUM -> HIGH.",
    ),
    "COR-02": CorroborationRule(
        rule_id="COR-02",
        primary_signal="Any nCino detector fires (Salesforce)",
        corroborating_signal=(
            "Jira shows unresolved issues linked to the same process within 30 days"
        ),
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label="Jira",
        description="Corroborated by Jira issues. MEDIUM -> HIGH.",
    ),
    "COR-03": CorroborationRule(
        rule_id="COR-03",
        primary_signal="Any nCino detector fires (Salesforce)",
        corroborating_signal="Both ServiceNow AND Jira corroborate (COR-01 + COR-02 both true)",
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        # COR-03 is a derived rule: its sources are already contributed by
        # COR-01 and COR-02. It adds the triple-corroboration label, not a chip.
        source_label=None,
        description=(
            "Triple corroboration: Salesforce + ServiceNow + Jira. "
            "Derived when COR-01 and COR-02 both fire. HIGH + 'Triple corroboration'."
        ),
    ),
    "COR-04": CorroborationRule(
        rule_id="COR-04",
        primary_signal="COVENANT_TRACKING_GAP fires",
        corroborating_signal=(
            "Confluence shows no documentation for covenant review process "
            "in declared space"
        ),
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label="Confluence (no process documentation)",
        description="Process gap confirmed: no Confluence documentation. MEDIUM -> HIGH.",
    ),
    # ── Documentation cross-reference corroboration (Confluence / SharePoint) ──
    #
    # Note what these do NOT say. The obvious "documentation exists about this
    # process, therefore the finding is corroborated" is deliberately not a rule:
    # the existence of an approvals page is no evidence that approvals are slow,
    # and almost every org has documentation about almost every process, so such a
    # rule would fire on nearly everything and mean nearly nothing.
    #
    # An EXPLICIT reference is different in kind. A page or document that cites
    # INC-4821 by number is demonstrably about the same incident the detector fired
    # on — the org's own knowledge base connecting the friction to a written record.
    # That is a real second source, and it is exactly the marker both connectors
    # have always extracted (Confluence page titles, SharePoint file names) and,
    # until now, discarded unused.
    #
    # Both are `elevates=True` with a HIGH target: the ceiling on documentation is
    # its ROLE WEIGHT (documentation_system 0.6 < the 0.80 lead threshold), which
    # lets it contribute to and be credited on an elevation another source leads
    # while never reaching HIGH alone. That is deliberately a different mechanism
    # from COR-05's hard `elevates=False` Slack ceiling.
    "COR-11": CorroborationRule(
        rule_id="COR-11",
        primary_signal="Any detector fires",
        corroborating_signal=(
            "A Confluence page explicitly references a ServiceNow incident or Jira "
            "issue that is itself linked to this detector"
        ),
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label="Confluence (references the same record)",
        description=(
            "Documentation cites the corroborating record by id. Supports an "
            "elevation; cannot lead one (documentation_system role weight)."
        ),
    ),
    "COR-12": CorroborationRule(
        rule_id="COR-12",
        primary_signal="Any detector fires",
        corroborating_signal=(
            "A SharePoint document explicitly references a ServiceNow incident or "
            "Jira issue that is itself linked to this detector"
        ),
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label="SharePoint (references the same record)",
        description=(
            "Documentation cites the corroborating record by id. Supports an "
            "elevation; cannot lead one (documentation_system role weight)."
        ),
    ),
    "COR-05": CorroborationRule(
        rule_id="COR-05",
        primary_signal="Any operational detector fires",
        corroborating_signal="Slack ESCALATION_PATTERN also fires in same run",
        # SLACK CEILING: Slack alone stays MEDIUM. Never elevate Slack-only.
        elevation_target=CONFIDENCE_MEDIUM,
        elevates=False,
        source_label="Slack (supporting only)",
        description=(
            "Supporting signal: Slack escalation pattern detected. "
            "MEDIUM stays MEDIUM — Slack elevates only with another corroborator. "
            "HARDCODED PRINCIPLE: Slack-only must never reach HIGH."
        ),
    ),
    "COR-06": CorroborationRule(
        rule_id="COR-06",
        primary_signal="Any operational detector fires",
        corroborating_signal=(
            "Slack ESCALATION_PATTERN fires AND ServiceNow corroborates (COR-01 true)"
        ),
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label="Slack (escalation pattern)",
        description=(
            "Corroborated by ServiceNow + Slack escalation pattern. MEDIUM -> HIGH. "
            "Slack contributes to elevation only when a primary corroborator is present."
        ),
    ),
    "COR-07": CorroborationRule(
        rule_id="COR-07",
        primary_signal="LOAN_ORIGINATION_BOTTLENECK fires",
        corroborating_signal=(
            "Jira shows sprint velocity drop in the same engineering team "
            "over same period"
        ),
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label="Jira (sprint velocity decline)",
        description="Corroborated by Jira sprint velocity decline. MEDIUM -> HIGH.",
    ),
    "COR-08": CorroborationRule(
        rule_id="COR-08",
        primary_signal="Any detector fires",
        corroborating_signal="Only one system connected",
        # Single source cannot self-corroborate — no elevation, no source chip.
        elevation_target=CONFIDENCE_MEDIUM,
        elevates=False,
        source_label=None,
        description=(
            "Single source — connect additional systems for higher confidence. "
            "No elevation: a single system cannot self-corroborate."
        ),
    ),
    "COR-09": CorroborationRule(
        rule_id="COR-09",
        primary_signal="Any detector fires",
        corroborating_signal=(
            "A Java application shows operational friction within 30 days "
            "(rising error rate, latency degradation, resource pressure, or a "
            "recurring exception cluster) for the same service"
        ),
        # R17-A3 §3: operational signals are DIRECTLY MEASURED, so they are
        # first-class OBSERVED evidence — not inferred content subject to the
        # inferred-edge discipline. Java-app operational friction therefore
        # elevates exactly as a system-of-record corroborator does (unlike the
        # Slack/Teams conversation-source ceiling in COR-05), e.g. an error-rate
        # rise corroborating a ServiceNow incident spike for the same service.
        #
        # Single-source ceiling (reviewed & intentional — R17-A3, M3): COR-09
        # elevates a finding that ALREADY exists from another connected system,
        # exactly like COR-01 (ServiceNow) / COR-02 (Jira). It is deliberately
        # NOT subject to the conversation-source MEDIUM ceiling. This does NOT
        # mean a Java app alone can reach HIGH: a run with only ``java_app``
        # connected has no finding to corroborate and is capped at MEDIUM by the
        # single-source rule (COR-08) — there is no self-corroboration. HIGH is
        # reached only when the Java signal co-fires with the finding's own
        # (non-Java) source, i.e. two independent systems agree. Treating one
        # observed operational corroborator as sufficient to elevate is the same
        # bar applied to a single ServiceNow/Jira corroborator and is the
        # intended design: measured runtime behaviour is trustworthy evidence.
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label="Java application (operational signal)",
        description=(
            "Corroborated by Java application operational signal "
            "(framework health/diagnostics + logs). MEDIUM -> HIGH. "
            "Operational signals are observed evidence (R17-A3)."
        ),
    ),
    "COR-10": CorroborationRule(
        rule_id="COR-10",
        primary_signal="Any detector fires",
        corroborating_signal=(
            "A .NET application shows operational friction within 30 days "
            "(rising error rate, latency degradation, resource pressure, or a "
            "recurring exception cluster) for the same service"
        ),
        # R17-A4 §3: the .NET counterpart to COR-09. A .NET application's operational
        # signal is DIRECTLY MEASURED from its runtime logs/diagnostics, so — exactly
        # like Java — it is first-class OBSERVED evidence and elevates as a
        # system-of-record corroborator does (unlike the Slack/Teams conversation-
        # source ceiling). This is NOT a separate .NET confidence model: it plugs
        # into the same cross-system corroboration approach. The single-source
        # ceiling still applies — a run with only ``dotnet_app`` connected has no
        # finding to corroborate and stays MEDIUM (COR-08); HIGH is reached only when
        # the .NET signal co-fires with the finding's own (non-.NET) source.
        elevation_target=CONFIDENCE_HIGH,
        elevates=True,
        source_label=".NET application (operational signal)",
        description=(
            "Corroborated by .NET application operational signal "
            "(health/diagnostics + EventCounters + logs). MEDIUM -> HIGH. "
            "Operational signals are observed evidence (R17-A4)."
        ),
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Labels referenced by the engine when building the on-card corroboration label
# ─────────────────────────────────────────────────────────────────────────────

#: Exact label required by ENT-2 AC3 for triple corroboration.
TRIPLE_CORROBORATION_LABEL = "Triple corroboration: Salesforce + ServiceNow + Jira"

#: Label shown when only one system is connected (COR-08).
SINGLE_SOURCE_LABEL = "Single source — connect additional systems for higher confidence"

#: Per-rule on-card labels (ENT-2 Section 1a "Corroboration label on card").
RULE_CARD_LABELS: Dict[str, str] = {
    "COR-01": "Corroborated by ServiceNow incidents",
    "COR-02": "Corroborated by Jira issues",
    "COR-03": TRIPLE_CORROBORATION_LABEL,
    "COR-04": "Process gap confirmed: no Confluence documentation",
    "COR-11": "Referenced in Confluence documentation",
    "COR-12": "Referenced in SharePoint documentation",
    "COR-05": "Supporting signal: Slack escalation pattern detected",
    "COR-06": "Corroborated by ServiceNow + Slack escalation pattern",
    "COR-07": "Corroborated by Jira sprint velocity decline",
    "COR-08": SINGLE_SOURCE_LABEL,
    "COR-09": "Corroborated by Java application operational signal",
    "COR-10": "Corroborated by .NET application operational signal",
}


# ─────────────────────────────────────────────────────────────────────────────
# Audit / lookup helpers — read-only access to the registry
# ─────────────────────────────────────────────────────────────────────────────

def get_rule(rule_id: str) -> Optional[CorroborationRule]:
    """Return the rule definition for rule_id, or None if unknown."""
    return CORROBORATION_RULES.get(rule_id)


def list_rule_ids() -> List[str]:
    """Return all defined rule IDs in registry order."""
    return list(CORROBORATION_RULES.keys())


def is_elevating_rule(rule_id: str) -> bool:
    """Return True if rule_id is permitted to raise confidence.

    COR-05 (Slack-only) and COR-08 (single-source) return False — they are
    supporting/informational only and must never elevate confidence.
    """
    rule = CORROBORATION_RULES.get(rule_id)
    return bool(rule and rule.elevates)


def elevation_target_for(rule_id: str) -> str:
    """Return the confidence level rule_id elevates to, or MEDIUM if unknown."""
    rule = CORROBORATION_RULES.get(rule_id)
    return rule.elevation_target if rule else CONFIDENCE_MEDIUM
