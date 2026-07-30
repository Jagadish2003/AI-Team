"""2.0-A1 T1 — the measured-signal registry behind every projection.

The story names five signal *concepts*: queue volume, ageing, recurrence count,
time-to-resolve, and reassignment hops.  This codebase does not have fields with
those names.  What it does have is a per-detector ``SIGNAL_METRICS`` list of
REAL measured field names, snapshotted into ``signal_snapshots`` and therefore
baseline-able and re-measurable by 2.0-A2.

This module is the single, explicit mapping between the two — concept ⇒ the real
field name that carries it for a given detector.  A projection may only cite a
signal that appears here, so it can never claim movement on a signal the
platform does not actually measure.

Two invariants a structural test pins:

* Every ``movement_signal`` and every ``volume_signal`` named here must appear in
  the owning detector module's ``SIGNAL_METRICS`` list.
* Every detector with a ``SIGNAL_METRICS`` list must have a profile here, so a
  new detector cannot silently ship without a projection.

The ``manual_step`` string is the human step the agent is expected to replace,
written in intervention language ("triaging …", "chasing …") — never in
guaranteed-savings language.  It is pack-terminology-agnostic on purpose: the
terminology layer rewrites titles, not this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# --------------------------------------------------------------------------
# Signal concepts — the vocabulary the story speaks.
# --------------------------------------------------------------------------

CONCEPT_QUEUE_VOLUME = "queue_volume"
CONCEPT_AGEING = "ageing"
CONCEPT_RECURRENCE = "recurrence_count"
CONCEPT_TIME_TO_RESOLVE = "time_to_resolve"
CONCEPT_REASSIGNMENT = "reassignment_hops"

#: Concept id -> human label used on the API/UI surface.
SIGNAL_CONCEPTS: Dict[str, str] = {
    CONCEPT_QUEUE_VOLUME: "Queue volume",
    CONCEPT_AGEING: "Ageing",
    CONCEPT_RECURRENCE: "Recurrence count",
    CONCEPT_TIME_TO_RESOLVE: "Time to resolve",
    CONCEPT_REASSIGNMENT: "Reassignment hops",
}


@dataclass(frozen=True)
class DetectorSignalProfile:
    """What a given detector's finding is about, in projection terms.

    Attributes
    ----------
    concept:
        Which of the five story concepts this detector's primary signal carries.
    movement_signal:
        The REAL measured field name that should move if the agent is built.
        Must be a member of the detector's ``SIGNAL_METRICS``.
    volume_signal:
        The REAL measured field name carrying the population the finding is
        drawn from (the denominator).  Drives band width via sample size.
        ``None`` when the detector has no meaningful denominator.
    instance_signal:
        The REAL measured field name carrying the count of affected instances
        (the numerator) — "the N recurring cases" the recommendation names.
        Falls back to ``movement_signal`` when the detector's primary metric IS
        the count.
    manual_step:
        The manual step the agent would replace, in intervention language.
    unit:
        Unit of ``movement_signal`` — one of "count", "days", "hours", "ratio",
        "pct".  Used for wording, never for arithmetic.
    lower_is_better:
        True when a reduction in ``movement_signal`` is the improvement.  Every
        current detector fires on a problem, so this is True throughout; it is
        explicit so a future "coverage" detector cannot be mis-projected.
    """

    concept: str
    movement_signal: str
    manual_step: str
    unit: str = "count"
    volume_signal: Optional[str] = None
    instance_signal: Optional[str] = None
    lower_is_better: bool = True
    #: 2.0-A1 T5 — what the affected instances ARE, as a plural noun phrase, so
    #: the recommendation can say "the 240 recurring reassignment cases" rather
    #: than the generic "the 240 recurring cases". Domain wording, not pack
    #: terminology: the terminology layer rewrites titles, not this.
    case_noun: str = "recurring cases"
    #: What stays with a person after the agent takes the manual step. Every
    #: recommendation must state a residual — an agent that leaves nothing to
    #: judgement is a claim this platform does not make.
    residual: str = "exceptions and cases that need judgement"

    #: Units that express a RATE rather than a countable population. A field in
    #: these units can never stand in for an affected-instance count.
    RATE_UNITS = ("ratio", "pct")

    @property
    def instance_field(self) -> Optional[str]:
        """The field carrying the affected-instance count, if the detector has one.

        Falls back to ``movement_signal`` only when that signal is itself a count:
        for a rate-based detector (breach rate, surge ratio, author share) there is
        no instance count on the record, and returning the rate would let a value
        like ``0.42`` be read as "0.42 affected instances".
        """
        if self.instance_signal:
            return self.instance_signal
        if self.unit in self.RATE_UNITS:
            return None
        return self.movement_signal


# --------------------------------------------------------------------------
# Per-detector profiles.
#
# Field names are copied from each detector's own SIGNAL_METRICS list — see
# backend/discovery/detectors/<module>.py.  Keep this table and those lists in
# lockstep; test_signal_registry.py fails the build if they drift.
# --------------------------------------------------------------------------

_PROFILES: Dict[str, DetectorSignalProfile] = {
    # ---- Service Cloud / core Salesforce ---------------------------------
    "REPETITIVE_AUTOMATION": DetectorSignalProfile(
        concept=CONCEPT_RECURRENCE,
        movement_signal="records_90d",
        volume_signal="records_90d",
        instance_signal="records_90d",
        manual_step="manually repeating the same record-update sequence on each new record",
        unit="count",
        case_noun="recurring record-update sequences",
        residual="records whose handling does not match a known pattern",
    ),
    "HANDOFF_FRICTION": DetectorSignalProfile(
        concept=CONCEPT_REASSIGNMENT,
        movement_signal="owner_changes_90d",
        volume_signal="total_cases_90d",
        instance_signal="owner_changes_90d",
        manual_step="manually re-routing cases between queues to find the right owner",
        unit="count",
        case_noun="recurring reassignment cases",
        residual="cases whose correct owner is genuinely ambiguous",
    ),
    "KNOWLEDGE_GAP": DetectorSignalProfile(
        concept=CONCEPT_RECURRENCE,
        movement_signal="closed_cases_90d",
        volume_signal="closed_cases_90d",
        instance_signal="closed_cases_90d",
        manual_step="re-deriving an answer that an existing resolution already documents",
        unit="count",
        case_noun="recurring resolutions already documented elsewhere",
        residual="novel resolutions with no documented precedent",
    ),
    "APPROVAL_BOTTLENECK": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="max_cycle_days",
        volume_signal="total_instances",
        instance_signal="pending_count",
        manual_step="chasing approvers by hand to move a pending approval forward",
        unit="days",
        case_noun="pending approvals waiting on a chase",
        residual="approval decisions themselves, which stay with the approver",
    ),
    "PERMISSION_BOTTLENECK": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="pending_count",
        volume_signal="process_count",
        instance_signal="pending_count",
        manual_step="manually locating an available approver when the named approver is unavailable",
        unit="count",
        case_noun="queued items waiting on an available approver",
        residual="approval decisions themselves, which stay with the approver",
    ),
    "INTEGRATION_CONCENTRATION": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="max_flow_reference_count",
        volume_signal="credential_count",
        instance_signal="firing_credential_count",
        manual_step="hand-tracing which automations depend on a shared integration credential",
        unit="count",
        case_noun="automations sharing one integration credential",
        residual="credential and governance changes, which stay with an owner",
    ),
    "CROSS_SYSTEM_ECHO": DetectorSignalProfile(
        concept=CONCEPT_RECURRENCE,
        movement_signal="sf_echo_count",
        volume_signal="sf_total_cases",
        instance_signal="sf_echo_count",
        manual_step="re-entering the same issue in a second system and reconciling the two by hand",
        unit="count",
        case_noun="issues duplicated across two systems",
        residual="records whose two sides genuinely disagree",
    ),
    # ---- nCino lending ---------------------------------------------------
    "LOAN_ORIGINATION_ROUTING_FRICTION": DetectorSignalProfile(
        concept=CONCEPT_REASSIGNMENT,
        movement_signal="max_owner_changes",
        volume_signal="total_loans",
        instance_signal="high_friction_count",
        manual_step="manually reassigning a loan between owners as it moves through stages",
        unit="count",
        case_noun="loans reassigned between owners",
        residual="credit judgement and any lending decision, which stay with a banker",
    ),
    "SPREADING_BOTTLENECK": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="max_days_unlocked",
        volume_signal="total_periods",
        instance_signal="unlocked_count",
        manual_step="manually chasing and finalising open spread statement periods",
        unit="days",
        case_noun="open spread statement periods",
        residual="the spreading analysis itself, which stays with an analyst",
    ),
    "CHECKLIST_BOTTLENECK": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="max_overrun_days",
        volume_signal="total_checklists",
        instance_signal="overrun_count",
        manual_step="manually tracking which checklist items have overrun their duration",
        unit="days",
        case_noun="checklist items past their expected duration",
        residual="items blocked on a judgement a person must make",
    ),
    "COVENANT_TRACKING_GAP": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="max_days_past_evaluation",
        volume_signal="total_covenants",
        instance_signal="overdue_count",
        manual_step="manually reviewing covenant schedules to find evaluations that have come due",
        unit="days",
        case_noun="covenant evaluations that have come due",
        residual="the covenant assessment itself, which stays with a credit officer",
    ),
    "APPLICATION_STALL": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="max_days_stalled",
        volume_signal="total_applications",
        instance_signal="stalled_count",
        manual_step="manually identifying and restarting applications that have gone quiet",
        unit="days",
        case_noun="stalled retirement applications",
        residual="the eligibility determination, which stays with a benefits officer",
    ),
    # ---- STRS benefits ---------------------------------------------------
    "DISABILITY_REVIEW_BOTTLENECK": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="max_days_pending",
        volume_signal="total_disability_cases",
        instance_signal="pending_review_count",
        manual_step="manually tracking which disability reviews are still awaiting a decision",
        unit="days",
        case_noun="disability reviews awaiting a decision",
        residual="the medical and eligibility decision, which stays with a reviewer",
    ),
    "DISBURSEMENT_OVERDUE": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="max_days_overdue",
        volume_signal="total_assignments",
        instance_signal="overdue_count",
        manual_step="manually reconciling disbursement schedules to find overdue payments",
        unit="days",
        case_noun="overdue disbursements",
        residual="payment authorisation, which stays with a benefits officer",
    ),
    "BENEFIT_ELECTION_DEADLINE": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="max_days_overdue",
        volume_signal="total_assignments",
        instance_signal="overdue_election_count",
        manual_step="manually checking benefit elections against their deadlines",
        unit="days",
        case_noun="benefit elections past their deadline",
        residual="member contact and any election decision, which stay with staff",
    ),
    # ---- SQL Server operational signal ----------------------------------
    "DB_QUEUE_DEPTH_ELEVATED": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="p1_p2_open",
        volume_signal="total_open",
        instance_signal="p1_p2_open",
        manual_step="manually triaging the open high-priority ticket queue to decide what is worked next",
        unit="count",
        case_noun="open high-priority tickets awaiting triage",
        residual="tickets needing a judgement call on priority or ownership",
    ),
    "DB_SLA_BREACH_RATE": DetectorSignalProfile(
        concept=CONCEPT_TIME_TO_RESOLVE,
        movement_signal="breach_rate_pct",
        volume_signal="total_tickets_30d",
        instance_signal="breached_count",
        manual_step="manually watching in-flight tickets to catch one before it breaches its SLA",
        unit="pct",
        case_noun="tickets tracking toward an SLA breach",
        residual="the remediation work itself, which stays with the assignee",
    ),
    "DB_TICKET_VOLUME_SURGE": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="recent_vs_baseline",
        volume_signal="total_90d",
        # recent_7d_avg is a daily AVERAGE, not a count of affected instances —
        # using it as one would understate the population by ~7x. The 90-day
        # total is the honest population; the affected count is not measured.
        instance_signal=None,
        manual_step="manually absorbing an intake surge by re-prioritising the queue by hand",
        unit="ratio",
        case_noun="tickets arriving in the surge window",
        residual="prioritisation calls that depend on business context",
    ),
    # ---- Enterprise ops (ServiceNow / Jira) -----------------------------
    "ENT_INCIDENT_RESOLUTION_LAG": DetectorSignalProfile(
        concept=CONCEPT_TIME_TO_RESOLVE,
        movement_signal="avg_lag_days",
        volume_signal="incident_count_30d",
        instance_signal="unresolved_count",
        manual_step="manually chasing the linked engineering issue after an incident is closed",
        unit="days",
        case_noun="incidents awaiting their linked engineering issue",
        residual="the engineering fix itself, which stays with the team",
    ),
    "ENT_SLA_BREACH_BY_TEAM": DetectorSignalProfile(
        concept=CONCEPT_TIME_TO_RESOLVE,
        movement_signal="top_team_breach_rate",
        volume_signal="teams_analysed",
        # This detector measures RATES, not counts — it reports no breached-ticket
        # count. instance_signal is deliberately None (rather than the tempting
        # top_team_breach_pct, which is a share and would be misread as an
        # instance count); the projection then bands off the team population.
        instance_signal=None,
        manual_step="manually rebalancing work across teams once breaches concentrate in one queue",
        unit="ratio",
        case_noun="breaching tickets in the concentrated queue",
        residual="rebalancing decisions, which stay with a team lead",
    ),
    "ENT_CHANGE_INCIDENT_CORRELATION": DetectorSignalProfile(
        concept=CONCEPT_RECURRENCE,
        movement_signal="post_change_incidents",
        volume_signal="change_count_30d",
        instance_signal="post_change_incidents",
        manual_step="manually correlating new incidents back to the change that preceded them",
        unit="count",
        case_noun="incidents following a recorded change",
        residual="the causal assessment, which stays with a change owner",
    ),
    # ---- GitHub engineering ---------------------------------------------
    "GITHUB_PR_REVIEW_BOTTLENECK": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="avg_days_open",
        volume_signal="open_pr_count",
        instance_signal="prs_over_threshold",
        manual_step="manually chasing reviewers to move a waiting pull request forward",
        unit="days",
        case_noun="pull requests waiting past the review threshold",
        residual="the code review itself, which stays with a reviewer",
    ),
    "GITHUB_STALE_BRANCHES": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="oldest_stale_days",
        volume_signal="total_branches",
        instance_signal="stale_count",
        manual_step="manually auditing branches to find which are abandoned",
        unit="days",
        case_noun="branches with no recent activity",
        residual="the decision to delete or revive a branch, which stays with its owner",
    ),
    "GITHUB_COMMIT_CONCENTRATION": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="top_author_pct",
        volume_signal="total_contributors",
        # A share, not a count — the contributor population is the honest basis.
        instance_signal=None,
        manual_step="manually spreading review and change load when it concentrates on one contributor",
        unit="ratio",
        case_noun="changes concentrated on one contributor",
        residual="the engineering work itself, which stays with the team",
    ),
    # ---- MSP cloud-ops (ServiceNow ITSM + cloud events) -------------------
    "ALERT_TRIAGE_TOIL": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="incident_volume",
        volume_signal="event_count",
        instance_signal="incident_volume",
        manual_step="manually triaging repetitive alerts to decide which need action",
        unit="count",
        case_noun="repetitive alerts requiring triage",
        residual="alerts needing a judgement call on severity or ownership",
    ),
    "QUEUE_AGEING": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="current_avg_age_hours",
        volume_signal="open_count",
        instance_signal="open_count",
        manual_step="manually chasing ageing tickets to keep the queue moving",
        unit="hours",
        case_noun="ageing tickets in the queue",
        residual="tickets blocked on a decision a person must make",
    ),
    "REASSIGNMENT_PING_PONG": DetectorSignalProfile(
        concept=CONCEPT_REASSIGNMENT,
        movement_signal="hop_count",
        volume_signal="groups_involved",
        instance_signal="hop_count",
        manual_step="manually re-routing an incident between groups to find the right owner",
        unit="count",
        case_noun="reassignment hops between groups",
        residual="incidents whose correct owner is genuinely ambiguous",
    ),
    "RECURRING_RESOLUTION_LOOP": DetectorSignalProfile(
        concept=CONCEPT_RECURRENCE,
        movement_signal="recurrence_count",
        # corroborated_count is a corroboration tally, not the population the
        # finding is drawn from, so there is no honest denominator here.
        volume_signal=None,
        instance_signal="recurrence_count",
        manual_step="re-applying the same resolution each time the issue returns",
        unit="count",
        case_noun="recurring incidents resolved the same way each time",
        residual="recurrences with a genuinely new cause",
    ),
    "SHARED_CI_HOTSPOT": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="incident_count",
        volume_signal="service_count",
        instance_signal="common_ci_count",
        manual_step="manually tracing which services share the configuration item behind an incident",
        unit="count",
        case_noun="incidents concentrated on a shared configuration item",
        residual="remediation decisions on the shared CI, which stay with its owner",
    ),
    "OPS_RUNBOOK_DOCUMENTATION_GAP": DetectorSignalProfile(
        concept=CONCEPT_RECURRENCE,
        movement_signal="documentation_gap_count",
        volume_signal="recurrence_count",
        instance_signal="documentation_gap_count",
        manual_step="re-deriving a fix that has no runbook entry to follow",
        unit="count",
        case_noun="recurring incidents with no runbook coverage",
        residual="incidents needing a new runbook written by an engineer",
    ),
    "OPS_REASSIGNMENT_PING_PONG": DetectorSignalProfile(
        concept=CONCEPT_REASSIGNMENT,
        movement_signal="ping_pong_incident_count",
        volume_signal=None,
        instance_signal="ping_pong_incident_count",
        manual_step="manually handing an incident between groups to find the right owner",
        unit="count",
        case_noun="incidents bounced between assignment groups",
        residual="incidents whose correct owner is genuinely ambiguous",
    ),
    "OPS_RESOLUTION_RECURRENCE": DetectorSignalProfile(
        concept=CONCEPT_RECURRENCE,
        movement_signal="recurrence_loop_count",
        volume_signal=None,
        instance_signal="recurrence_loop_count",
        manual_step="re-resolving an incident a previous resolution already addressed",
        unit="count",
        case_noun="recurring resolution loops",
        residual="incidents whose recurrence has a genuinely new cause",
    ),
    # ---- MSP security-ops ------------------------------------------------
    # Group/queue-level language only: the 1.9 aggregation floor forbids naming
    # an individual or enumerating host x vulnerability in any output surface.
    "SECOPS_SIR_TRIAGE_TOIL": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="incident_volume",
        # category_count counts CATEGORIES, not incidents — using it as the
        # population would understate the sample by orders of magnitude.
        volume_signal=None,
        instance_signal="incident_volume",
        manual_step="manually triaging repetitive security incidents to classify them",
        unit="count",
        case_noun="repetitive security incidents requiring triage",
        residual="incidents needing an analyst's judgement on severity",
    ),
    "SECOPS_SLA_DEFERRAL_AGEING": DetectorSignalProfile(
        concept=CONCEPT_AGEING,
        movement_signal="current_avg_age_seconds",
        volume_signal="open_count",
        instance_signal="open_count",
        manual_step="manually chasing deferred security findings before they breach SLA",
        unit="seconds",
        case_noun="deferred security findings ageing toward SLA breach",
        residual="deferral decisions, which stay with a security owner",
    ),
    "SECOPS_SECURITY_IT_PING_PONG": DetectorSignalProfile(
        concept=CONCEPT_REASSIGNMENT,
        movement_signal="ping_pong_record_count",
        volume_signal=None,
        instance_signal="ping_pong_record_count",
        manual_step="manually handing a record back and forth between security and IT",
        unit="count",
        case_noun="records passed back and forth between security and IT",
        residual="records whose correct owning team is genuinely ambiguous",
    ),
    "SECOPS_REMEDIATION_RECURRENCE": DetectorSignalProfile(
        concept=CONCEPT_RECURRENCE,
        movement_signal="recurrence_count",
        volume_signal="observed_cycles",
        instance_signal="loops_found",
        manual_step="re-running the same remediation each time the finding returns",
        unit="count",
        case_noun="recurring remediation loops",
        residual="findings whose recurrence reflects a new exposure",
    ),
    "SECOPS_SHARED_INFRA_CONCENTRATION": DetectorSignalProfile(
        concept=CONCEPT_QUEUE_VOLUME,
        movement_signal="hotspot_count",
        volume_signal="workload_count",
        instance_signal="hotspot_count",
        manual_step="manually tracing which workloads share the infrastructure behind a finding",
        unit="count",
        case_noun="findings concentrated on shared infrastructure",
        residual="remediation decisions on shared infrastructure, which stay with an owner",
    ),
}


def get_detector_profile(detector_id: str) -> Optional[DetectorSignalProfile]:
    """Return the projection profile for ``detector_id``, or None if unmapped.

    An unmapped detector yields no projection rather than a guessed one — a
    projection about an invented signal is worse than no projection.
    """
    if not detector_id:
        return None
    return _PROFILES.get(str(detector_id).strip().upper())


def known_detector_ids() -> Tuple[str, ...]:
    """Detector ids with a projection profile, sorted for determinism."""
    return tuple(sorted(_PROFILES))


__all__ = [
    "CONCEPT_AGEING",
    "CONCEPT_QUEUE_VOLUME",
    "CONCEPT_RECURRENCE",
    "CONCEPT_REASSIGNMENT",
    "CONCEPT_TIME_TO_RESOLVE",
    "SIGNAL_CONCEPTS",
    "DetectorSignalProfile",
    "get_detector_profile",
    "known_detector_ids",
]
