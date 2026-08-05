"""2.0-B4 T3 — source → normalised-concept mappers (AT-812).

T1 defined *what* the concepts are and *which* connector must map onto them; it
built no mapper on purpose ("Turning a ServiceNow incident into a ``WorkItem`` is
T2/T3"). T3's job — the detector-portability proof (AC2) — needs concept instances
to feed a ported detector, so this module supplies the first real mappers: the ones
the ported detectors in :mod:`~discovery.concepts.portable_detectors` read.

Scope discipline. A mapper here converts the *detector-visible* shape of a source
(the block a detector already reads, e.g. ``sf_data['approval_processes']``) onto
the concept set. It is deliberately NOT the connector's ingestion path, and it does
NOT flip that connector's conformance declaration to ``supported`` — the conformance
registry tracks the shipping ingest mapper, which is T2's remit. Keeping the two
apart is the same honesty the conformance module insists on: a proof mapper is not a
shipped mapper.

What "normalised" buys, concretely. A detector reading these concepts never names
``approval_processes`` or ``bottleneck_score`` as a Salesforce field path — it reads
an :class:`~discovery.concepts.model.Approval` and the
:class:`~discovery.concepts.model.ActorGroup` its ``approver_group`` points at. The
source's own pre-computed measurements (``avg_delay_days``, ``bottleneck_score``)
ride on the concept's ``attributes`` bag — B0's ``payload`` rule: carry
source-specific detail without leaking a source-specific SHAPE into the contract.
The structural facts a detector reasons over — *this is an approval*, *its approvers
are a group of N* — are normalised into first-class concept fields, which is exactly
what lets one concept-native detector later (AC3) run across source families.
"""

from __future__ import annotations

from typing import Any, Dict, List

try:
    from app.provenance import EvidencePointer
    from discovery.concepts.model import (
        ActorGroup,
        Approval,
        ConceptSignal,
        EntityReference,
        WorkItem,
    )
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.provenance import EvidencePointer
    from backend.discovery.concepts.model import (
        ActorGroup,
        Approval,
        ConceptSignal,
        EntityReference,
        WorkItem,
    )

#: A fixed, deterministic observation time for mapped concepts. The source shapes
#: these mappers read (a detector-visible block) carry no per-record observation
#: time, so one is stamped here. Deterministic on purpose: the portability proof
#: compares byte-for-byte, and a wall-clock read would make the mapping unstable.
MAPPER_OBSERVED_AT = "2026-01-01T00:00:00Z"

# The stable source_system for the Service Cloud / Salesforce family. The base
# Salesforce ingestor is the connector; the detectors that read approval processes
# stamp their findings ``signal_source='salesforce'``, so the concepts carry the
# same source_system and the ported detector reproduces it unchanged.
_SF_SOURCE_SYSTEM = "salesforce"


def _observed_provenance(source_system: str, source_artifact: str) -> Dict[str, Any]:
    """Build a valid OBSERVED EvidencePointer spine for a mapped concept.

    Every concept is a ``CommonSignal`` and refuses construction without a valid
    observed provenance (R16-B1 AC2). The mapping is directly measured from the
    source's own records, so ``observed`` is the correct and only honest origin.
    """
    return EvidencePointer.observed(
        source_system=source_system,
        source_artifact=source_artifact,
        source_timestamp=MAPPER_OBSERVED_AT,
    ).to_dict()


def _approver_group_key(source_system: str, source_record_id: str) -> tuple[str, str]:
    """The key a concept-native detector uses to resolve an ``Approval``'s
    ``approver_group`` reference to the ``ActorGroup`` carrying ``member_count``.

    Kept here so the mapper and the ported detectors cannot drift on how a
    reference is matched to its group."""
    return (str(source_system), str(source_record_id))


def actor_group_key(group: ActorGroup) -> tuple[str, str]:
    """The lookup key for an ``ActorGroup`` (its source system + name)."""
    return _approver_group_key(group.source_system, group.name)


def approver_group_ref_key(approval: Approval) -> tuple[str, str] | None:
    """The lookup key an ``Approval`` points at, or ``None`` if it names no group."""
    ref = approval.approver_group
    if ref is None:
        return None
    return _approver_group_key(ref.source_system, ref.source_record_id)


def map_service_cloud_approvals(
    sf_data: Dict[str, Any],
    *,
    org_id: str,
    observed_at: str = MAPPER_OBSERVED_AT,
) -> List[ConceptSignal]:
    """Map the Service Cloud ``approval_processes`` block onto normalised concepts.

    For each approval process the source reports, emit two concepts, in this order:

      1. an :class:`ActorGroup` — the approver group, ``group_type='role'``, with
         ``member_count`` = the process's ``approver_count`` (a normalised aggregate,
         never a roster); and
      2. an :class:`Approval` — the gate, ``decision='pending'`` when work is waiting
         on it else ``'approved'``, whose ``approver_group`` reference points back at
         the ActorGroup above.

    The source's pre-computed per-process measurements (``avg_delay_days``,
    ``bottleneck_score``, ``pending_count``) ride on the Approval's ``attributes``
    bag — the source computed them, so the faithful mapping carries them rather than
    fabricating per-approval detail the source never recorded.

    Order is preserved (process order, group-before-approval) so a concept-native
    detector reproduces the original detector's per-process findings in the same
    order — which byte-for-byte finding equality (AC2) requires.
    """
    if not org_id:
        raise ValueError("org_id is required — every concept is org-scoped")

    processes = sf_data.get("approval_processes") or []
    out: List[ConceptSignal] = []

    for ap in processes:
        process_name = str(ap.get("process_name") or "")
        # A stable, human-facing anchor the reference and the group share. Every
        # process in a real org has a name; guard the pathological empty case so a
        # blank name cannot silently collapse two processes into one group.
        anchor = process_name or f"approval-process-{len(out)}"

        approver_count = int(ap.get("approver_count", 0) or 0)
        pending_count = int(ap.get("pending_count", 0) or 0)
        avg_delay_days = float(ap.get("avg_delay_days", 0.0) or 0.0)
        bottleneck_score = float(ap.get("bottleneck_score", 0.0) or 0.0)

        provenance = _observed_provenance(_SF_SOURCE_SYSTEM, anchor)

        group = ActorGroup(
            org_id=org_id,
            source_system=_SF_SOURCE_SYSTEM,
            signal_id=f"approval-group:{anchor}",
            observed_at=observed_at,
            provenance=provenance,
            group_type="role",
            name=anchor,
            member_count=approver_count,
        )

        approver_ref = EntityReference(
            entity_type="team",
            source_system=_SF_SOURCE_SYSTEM,
            source_record_id=anchor,
            display_name=process_name or None,
        )

        approval = Approval(
            org_id=org_id,
            source_system=_SF_SOURCE_SYSTEM,
            signal_id=f"approval:{anchor}",
            observed_at=observed_at,
            provenance=provenance,
            decision="pending" if pending_count > 0 else "approved",
            approval_type="other",
            approver_group=approver_ref,
            attributes={
                "process_name": process_name,
                "avg_delay_days": avg_delay_days,
                "bottleneck_score": bottleneck_score,
                "pending_count": pending_count,
            },
        )

        out.append(group)
        out.append(approval)

    return out


# ── WorkItem mappers, one per source family (2.0-B4 T4 / AT-813) ────────────────
#
# The point AC3 exists to prove: a ``work_item`` is a ``work_item`` whatever dialect
# it arrived in. ServiceNow speaks ``state`` / ``assignment_group`` / ``opened_at``;
# Jira speaks ``fields.status.name`` / ``fields.assignee`` / ``created``; Salesforce
# speaks ``Status`` / ``OwnerGroup`` / ``CreatedDate``; GitHub speaks ``state`` /
# ``assignees`` / ``created_at``. Each mapper below normalises its dialect onto the
# ONE :class:`WorkItem` shape — coarse ``status_category`` with the native string kept
# on ``native_status`` — so a single concept-only detector reads all four unchanged.
#
# AC5 lives in the ``assigned_group`` line of each mapper. ServiceNow and Salesforce
# assign to GROUPS (``actor_group`` = declared), so the mapper sets ``assigned_group``.
# Jira and Salesforce... no — Jira and GitHub assign to INDIVIDUALS and declare an
# ``actor_group`` GAP, so their mappers leave ``assigned_group`` ``None``: a group is
# never synthesised from a person's name. The gap is recorded (conformance.py) and
# honoured here, never silently approximated.

def _normalise_token(native: Any, mapping: Dict[str, str], default: str = "other") -> str:
    """Look a native value up in a case-insensitive normalisation map."""
    return mapping.get(str(native or "").strip().lower(), default)


def _team_ref(source_system: str, name: Any) -> EntityReference | None:
    """A group reference, or None when the source names no group."""
    label = str(name or "").strip()
    if not label:
        return None
    return EntityReference(
        entity_type="team",
        source_system=source_system,
        source_record_id=label,
        display_name=label,
    )


def _work_item(
    *,
    org_id: str,
    source_system: str,
    signal_id: str,
    reference: str,
    work_item_type: str,
    status_category: str,
    native_status: Any,
    opened_at: Any,
    assigned_group: EntityReference | None,
    observed_at: str,
) -> WorkItem:
    if not org_id:
        raise ValueError("org_id is required — every concept is org-scoped")
    return WorkItem(
        org_id=org_id,
        source_system=source_system,
        signal_id=signal_id,
        observed_at=observed_at,
        provenance=_observed_provenance(source_system, reference or signal_id),
        work_item_type=work_item_type,
        status_category=status_category,
        native_status=str(native_status or ""),
        reference=reference,
        opened_at=str(opened_at) if opened_at else None,
        assigned_group=assigned_group,
    )


_SERVICENOW_STATUS = {
    "new": "open", "open": "open", "in progress": "in_progress",
    "on hold": "waiting", "pending": "waiting", "awaiting problem": "waiting",
    "resolved": "resolved", "closed": "closed",
    "cancelled": "cancelled", "canceled": "cancelled",
}
_SERVICENOW_TYPE = {
    "incident": "incident", "problem": "problem", "change_request": "change",
    "change": "change", "sc_req_item": "request", "request": "request",
}


def map_servicenow_work_items(
    records: List[Dict[str, Any]], *, org_id: str, observed_at: str = MAPPER_OBSERVED_AT
) -> List[WorkItem]:
    """ITSM (ServiceNow) incidents/requests → WorkItem. Groups supported."""
    out: List[WorkItem] = []
    for r in records or []:
        number = str(r.get("number") or r.get("sys_id") or "")
        out.append(_work_item(
            org_id=org_id,
            source_system="servicenow",
            signal_id=f"servicenow:work_item:{r.get('sys_id') or number}",
            reference=number,
            work_item_type=_normalise_token(
                r.get("sys_class_name") or r.get("type"), _SERVICENOW_TYPE, "incident"
            ),
            status_category=_normalise_token(r.get("state"), _SERVICENOW_STATUS),
            native_status=r.get("state"),
            opened_at=r.get("opened_at"),
            assigned_group=_team_ref("servicenow", r.get("assignment_group")),
            observed_at=observed_at,
        ))
    return out


_JIRA_STATUS = {
    "to do": "open", "open": "open", "backlog": "open",
    "selected for development": "open", "in progress": "in_progress",
    "in review": "in_progress", "blocked": "waiting", "waiting": "waiting",
    "done": "closed", "closed": "closed", "resolved": "resolved",
    "cancelled": "cancelled",
}
_JIRA_TYPE = {
    "bug": "issue", "story": "issue", "task": "task", "sub-task": "task",
    "epic": "task", "incident": "incident",
}


def map_jira_work_items(
    records: List[Dict[str, Any]], *, org_id: str, observed_at: str = MAPPER_OBSERVED_AT
) -> List[WorkItem]:
    """Engineering tracker (Jira) issues → WorkItem. AC5: Jira assigns to an
    individual and declares an actor_group GAP, so assigned_group stays None."""
    out: List[WorkItem] = []
    for r in records or []:
        fields = r.get("fields") or {}
        key = str(r.get("key") or "")
        status = fields.get("status")
        status_name = status.get("name") if isinstance(status, dict) else status
        itype = fields.get("issuetype")
        itype_name = itype.get("name") if isinstance(itype, dict) else itype
        out.append(_work_item(
            org_id=org_id,
            source_system="jira",
            signal_id=f"jira:work_item:{key}",
            reference=key,
            work_item_type=_normalise_token(itype_name, _JIRA_TYPE, "issue"),
            status_category=_normalise_token(status_name, _JIRA_STATUS),
            native_status=status_name,
            opened_at=fields.get("created"),
            assigned_group=None,  # actor_group GAP — never synthesise a group from an assignee
            observed_at=observed_at,
        ))
    return out


_SF_CASE_STATUS = {
    "new": "open", "open": "open", "working": "in_progress",
    "in progress": "in_progress", "escalated": "in_progress", "on hold": "waiting",
    "waiting": "waiting", "pending": "waiting", "resolved": "resolved",
    "closed": "closed", "cancelled": "cancelled",
}
_SF_CASE_TYPE = {
    "problem": "problem", "question": "request", "request": "request",
    "feature": "request",
}


def map_salesforce_cases(
    records: List[Dict[str, Any]], *, org_id: str, observed_at: str = MAPPER_OBSERVED_AT
) -> List[WorkItem]:
    """CRM (Salesforce) cases → WorkItem. Groups supported (case owner queue)."""
    out: List[WorkItem] = []
    for r in records or []:
        number = str(r.get("CaseNumber") or r.get("Id") or "")
        out.append(_work_item(
            org_id=org_id,
            source_system="salesforce",
            signal_id=f"salesforce:work_item:{r.get('Id') or number}",
            reference=number,
            work_item_type=_normalise_token(r.get("Type"), _SF_CASE_TYPE, "case"),
            status_category=_normalise_token(r.get("Status"), _SF_CASE_STATUS),
            native_status=r.get("Status"),
            opened_at=r.get("CreatedDate"),
            assigned_group=_team_ref("salesforce", r.get("OwnerGroup") or r.get("owner_group")),
            observed_at=observed_at,
        ))
    return out


_GITHUB_STATUS = {"open": "open", "closed": "closed"}


def map_github_issues(
    records: List[Dict[str, Any]], *, org_id: str, observed_at: str = MAPPER_OBSERVED_AT
) -> List[WorkItem]:
    """Code (GitHub) issues → WorkItem. AC5: GitHub assignees are individuals and it
    declares an actor_group GAP, so assigned_group stays None. Also shows the
    cancelled≠closed distinction: an issue closed as 'not planned' is cancelled."""
    out: List[WorkItem] = []
    for r in records or []:
        number = r.get("number")
        reference = f"#{number}" if number is not None else str(r.get("id") or "")
        state = r.get("state")
        if str(state or "").lower() == "closed" and str(
            r.get("state_reason") or ""
        ).lower() == "not_planned":
            status_category = "cancelled"
        else:
            status_category = _normalise_token(state, _GITHUB_STATUS)
        out.append(_work_item(
            org_id=org_id,
            source_system="github",
            signal_id=f"github:work_item:{reference}",
            reference=reference,
            work_item_type="issue",
            status_category=status_category,
            native_status=state,
            opened_at=r.get("created_at"),
            assigned_group=None,  # actor_group GAP — assignees are individuals
            observed_at=observed_at,
        ))
    return out


#: WorkItem mappers by source family (source_system → mapper). The set of families
#: a concept-only WorkItem detector can run across today (AC3 needs ≥3; this is 4).
WORK_ITEM_MAPPERS: Dict[str, Any] = {
    "servicenow": map_servicenow_work_items,
    "jira": map_jira_work_items,
    "salesforce": map_salesforce_cases,
    "github": map_github_issues,
}


__all__ = [
    "MAPPER_OBSERVED_AT",
    "map_service_cloud_approvals",
    "actor_group_key",
    "approver_group_ref_key",
    "map_servicenow_work_items",
    "map_jira_work_items",
    "map_salesforce_cases",
    "map_github_issues",
    "WORK_ITEM_MAPPERS",
]
