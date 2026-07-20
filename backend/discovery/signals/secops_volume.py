"""MSP-B11 T6 / AT-701 — Vulnerability-Response scan-cycle volume coordination.

A single ServiceNow scan cycle can create **thousands** of vulnerable-item updates
at once (RMF/FedRAMP ConMon, IAVA sweeps, KEV pushes). This module keeps that
burst **bounded and transparent** by running the SecOps streams through the SAME
volume-control disciplines MSP-B7 built for cloud events — it does NOT invent an
independent limit:

* **Budgeting** — the per-run counter :class:`~discovery.signals.budget.RunBudget`
  (reused verbatim), sized by the B7-calibrated
  :data:`~discovery.signals.budget.DEFAULT_RUN_EVENT_BUDGET`
  (``CALIBRATED_RUN_EVENT_BUDGET`` = 250 000). Once the budgeted window is full,
  every further record is **deferred-and-counted**, never silently dropped.
* **Reporting** — the loud-degradation proof :class:`~discovery.signals.budget.BudgetReport`
  (reused verbatim): processed vs deferred, the per-table deferred breakdown, and
  the deferred time window, plus a **safe checkpoint** the run can resume from.
* **Admission / folding** — the VR analogue of
  :class:`~discovery.signals.ops_stream.OpsEventStream` (whose ``admit`` is
  ``OperationalEvent``-only, so VR workflow signal cannot pass through it). Records
  fold into ONE :class:`WorkflowAggregate` per workflow key — so a scan that
  re-finds the same 200 CIs is a handful of workflow facts, not thousands. Memory
  is bounded by the number of distinct workflow patterns, never by record volume.

WORKLOAD, NOT WEAKNESS (AC6). The fold key IS MSP-B4/B11's deterministic
``remediation_signature`` — ``f(vulnerability_class, ci_class, remediation_path)``
— which by construction excludes host names, CVE/vulnerability ids, and sys_ids.
Aggregation therefore counts recurring class-level remediation workflow and
**cannot enumerate host×vulnerability pairs**: no aggregate this module emits
carries a host, a CVE, or a per-item id.

Deterministic + org-scoped (AC7). Folding depends only on record fields, never on
arrival order; the fold key includes ``org_id`` so two orgs never fold together
and a record is refused under a foreign org.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .budget import DEFAULT_RUN_EVENT_BUDGET, BudgetReport, RunBudget
from .remediation_signature import (
    compute_remediation_signature,
    remediation_path_from_history,
)
from .resolution_signature import normalize_token

# ── VR table families (mirror servicenow.py's SecOps constants) ──────────────

TABLE_VULNERABLE_ITEM = "sn_vul_vulnerable_item"
TABLE_VULNERABILITY_GROUP = "sn_vul_vulnerability_group"
TABLE_REMEDIATION_TASK = "sn_vul_remediation_task"
TABLE_SECURITY_INCIDENT = "sn_si_incident"

#: Every SecOps table family the volume coordinator understands.
SECOPS_TABLE_FAMILIES: Tuple[str, ...] = (
    TABLE_VULNERABLE_ITEM,
    TABLE_VULNERABILITY_GROUP,
    TABLE_REMEDIATION_TASK,
    TABLE_SECURITY_INCIDENT,
)

_SN_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class SecOpsOrgScopeError(ValueError):
    """Raised when a record is admitted under an org other than its own."""


def _text(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("value") or value.get("display_value") or value.get("name")
    if value is None:
        return None
    result = " ".join(str(value).strip().split())
    return result or None


def _parse_dt(value: Any) -> Optional[datetime]:
    """Tolerant UTC parse of a ServiceNow ``sys_updated_on`` (or ISO) timestamp.

    Returns ``None`` for an empty/unparseable value so a malformed timestamp
    never breaks admission — it just cannot move the deferred/aggregate window.
    """
    text = _text(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], _SN_DATETIME_FORMAT)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_ci_class(
    record: Mapping[str, Any], cmdb_index: Optional[Mapping[str, Mapping[str, Any]]]
) -> str:
    """Resolve the vulnerable item's CI class (B3 join), preferring a pre-resolved CI.

    Honours a ``resolved_ci`` already attached by the T3 CI-join; else looks the
    item's ``cmdb_ci`` sys_id up in the supplied B3 CMDB index; else empty
    (unlocated — the item still folds and counts, just without a CI class).
    """
    resolved = record.get("resolved_ci")
    if isinstance(resolved, Mapping) and resolved.get("ci_class"):
        return normalize_token(resolved.get("ci_class"))
    if cmdb_index:
        ci_id = _text(record.get("cmdb_ci"))
        if ci_id and ci_id in cmdb_index:
            return normalize_token(cmdb_index[ci_id].get("ci_class"))
    return ""


# ── workflow fold keys — one per table family, ALL classification-only ───────

def _vulnerable_item_workflow(
    record: Mapping[str, Any], cmdb_index: Optional[Mapping[str, Mapping[str, Any]]]
) -> Tuple[str, Dict[str, Any]]:
    """Fold a vulnerable item by its deterministic remediation workflow pattern.

    The fold identity is the MSP-B11 T4 ``remediation_signature`` (vuln class + CI
    class + remediation path) when all three are present — reusing the pack's one
    signature discipline verbatim — else a deterministic ``unsigned:`` fallback
    over the same axes so an unresolved item still folds without host/CVE detail.
    """
    vuln_class = normalize_token(record.get("vulnerability_class"))
    ci_class = _resolve_ci_class(record, cmdb_index)
    path = remediation_path_from_history(
        record.get("state_history"), current_state=record.get("state")
    )
    if vuln_class and ci_class and path:
        key = compute_remediation_signature(
            vulnerability_class=vuln_class, ci_class=ci_class, remediation_path=path
        )
    else:
        key = "unsigned:" + "\x1f".join((vuln_class, ci_class, "\x1e".join(path)))
    workflow = {
        "vulnerability_class": vuln_class,
        "ci_class": ci_class,
        "remediation_path": list(path),
    }
    return key, workflow


def _vulnerability_group_workflow(
    record: Mapping[str, Any], cmdb_index: Optional[Mapping[str, Mapping[str, Any]]]
) -> Tuple[str, Dict[str, Any]]:
    """Fold a vulnerability group by (vuln class, lifecycle state, assignment group)."""
    workflow = {
        "vulnerability_class": normalize_token(record.get("vulnerability_class")),
        "lifecycle_state": normalize_token(record.get("state")),
        "assignment_group": normalize_token(record.get("assignment_group")),
    }
    key = "group:" + "\x1f".join(
        (workflow["vulnerability_class"], workflow["lifecycle_state"], workflow["assignment_group"])
    )
    return key, workflow


def _remediation_task_workflow(
    record: Mapping[str, Any], cmdb_index: Optional[Mapping[str, Mapping[str, Any]]]
) -> Tuple[str, Dict[str, Any]]:
    """Fold a remediation task by (assignment group, lifecycle state)."""
    workflow = {
        "assignment_group": normalize_token(record.get("assignment_group")),
        "lifecycle_state": normalize_token(record.get("state")),
    }
    key = "task:" + "\x1f".join((workflow["assignment_group"], workflow["lifecycle_state"]))
    return key, workflow


def _security_incident_workflow(
    record: Mapping[str, Any], cmdb_index: Optional[Mapping[str, Mapping[str, Any]]]
) -> Tuple[str, Dict[str, Any]]:
    """Fold a security incident by (category, subcategory, close code, group)."""
    workflow = {
        "category": normalize_token(record.get("category")),
        "subcategory": normalize_token(record.get("subcategory")),
        "close_code": normalize_token(record.get("close_code")),
        "assignment_group": normalize_token(record.get("assignment_group")),
    }
    key = "sir:" + "\x1f".join(
        (workflow["category"], workflow["subcategory"], workflow["close_code"], workflow["assignment_group"])
    )
    return key, workflow


_WORKFLOW_KEYS = {
    TABLE_VULNERABLE_ITEM: _vulnerable_item_workflow,
    TABLE_VULNERABILITY_GROUP: _vulnerability_group_workflow,
    TABLE_REMEDIATION_TASK: _remediation_task_workflow,
    TABLE_SECURITY_INCIDENT: _security_incident_workflow,
}


# ── the folded, workflow-level unit (VR analogue of ActiveSignal) ────────────

@dataclass
class WorkflowAggregate:
    """One folded workflow pattern for a SecOps table family — counts, never hosts.

    Carries the aggregate's proof (member ``item_count``, first/last span, and the
    workflow-classification distributions) but NEVER a per-item identifier, host,
    or CVE — so it can describe effort concentration without enumerating
    host×vulnerability pairs (AC6).
    """

    table_family: str
    org_id: str
    fold_key: str
    workflow: Dict[str, Any]
    item_count: int = 0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    severity_bands: Dict[str, int] = field(default_factory=dict)
    assignment_groups: Dict[str, int] = field(default_factory=dict)
    lifecycle_states: Dict[str, int] = field(default_factory=dict)
    _first_dt: Optional[datetime] = field(default=None, repr=False)
    _last_dt: Optional[datetime] = field(default=None, repr=False)

    def _fold(self, record: Mapping[str, Any], dt: Optional[datetime], cursor: Optional[str]) -> None:
        self.item_count += 1
        severity = normalize_token(record.get("severity"))
        if severity:
            self.severity_bands[severity] = self.severity_bands.get(severity, 0) + 1
        group = normalize_token(record.get("assignment_group"))
        if group:
            self.assignment_groups[group] = self.assignment_groups.get(group, 0) + 1
        state = normalize_token(record.get("state"))
        if state:
            self.lifecycle_states[state] = self.lifecycle_states.get(state, 0) + 1
        if dt is not None:
            if self._first_dt is None or dt < self._first_dt:
                self._first_dt, self.first_seen = dt, cursor
            if self._last_dt is None or dt > self._last_dt:
                self._last_dt, self.last_seen = dt, cursor

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table_family": self.table_family,
            "org_id": self.org_id,
            "fold_key": self.fold_key,
            "workflow": dict(self.workflow),
            "item_count": self.item_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "severity_bands": dict(sorted(self.severity_bands.items())),
            "assignment_groups": dict(sorted(self.assignment_groups.items())),
            "lifecycle_states": dict(sorted(self.lifecycle_states.items())),
        }


@dataclass
class SecOpsAdmission:
    """The outcome of admitting one SecOps record.

    ``disposition`` is ``"new"`` (opened a workflow aggregate), ``"folded"`` (folded
    into an existing one), ``"deferred"`` (budget exhausted — deferred-and-counted,
    ``aggregate is None``), or ``"rejected"`` (missing sys_id / malformed — counted,
    never processed).
    """

    aggregate: Optional[WorkflowAggregate]
    disposition: str

    @property
    def is_deferred(self) -> bool:
        return self.disposition == "deferred"

    @property
    def is_rejected(self) -> bool:
        return self.disposition == "rejected"

    @property
    def is_processed(self) -> bool:
        return self.disposition in ("new", "folded")


@dataclass
class SecOpsVolumeMeasurements:
    """The measured VR volume picture — the run-health / B7-calibration hand-off.

    ``budget_report`` is the reused MSP-B7 :class:`BudgetReport` (the loud-deferral
    proof); ``safe_checkpoints`` is the resume cursor per table family (the last
    PROCESSED record's ``sys_updated_on`` — deferred records are strictly later, so
    a continuation fetches them without duplicating processed work).
    """

    budget: Optional[int]
    records_seen: int
    records_processed: int
    records_deferred: int
    records_rejected: int
    aggregate_count: int
    per_table: Dict[str, Dict[str, int]]
    safe_checkpoints: Dict[str, Optional[str]]
    budget_report: Dict[str, Any]
    workflow_aggregates: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "budget": self.budget,
            "records_seen": self.records_seen,
            "records_processed": self.records_processed,
            "records_deferred": self.records_deferred,
            "records_rejected": self.records_rejected,
            "aggregate_count": self.aggregate_count,
            "per_table": {k: dict(v) for k, v in sorted(self.per_table.items())},
            "safe_checkpoints": dict(sorted(self.safe_checkpoints.items())),
            "budget_report": dict(self.budget_report),
            "workflow_aggregates": [dict(a) for a in self.workflow_aggregates],
        }


# ── the admission stream (VR analogue of OpsEventStream) ─────────────────────

class SecOpsVolumeStream:
    """Bounds and folds SecOps scan-cycle volume at admission.

    Stateful over a run: :meth:`admit` folds each record into its workflow
    aggregate while the reused :class:`RunBudget` has capacity, and defers-and-counts
    every record past the budgeted window (loud, never silent). ``budget`` defaults
    to the B7-calibrated per-run budget; ``None`` means unbounded. One instance can
    serve many orgs — the aggregate key includes ``org_id``.
    """

    def __init__(
        self,
        *,
        budget: Optional[int] = DEFAULT_RUN_EVENT_BUDGET,
        cmdb_index: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ):
        self._budget = RunBudget(budget)
        self._budget_limit = budget
        self._cmdb_index = cmdb_index
        self._aggregates: Dict[Tuple[str, str, str], WorkflowAggregate] = {}
        self._seen = 0
        self._rejected = 0
        self._per_table: Dict[str, Dict[str, int]] = {}
        self._safe_checkpoints: Dict[str, Optional[str]] = {}
        self._safe_dt: Dict[str, datetime] = {}

    def _table_counts(self, table_family: str) -> Dict[str, int]:
        return self._per_table.setdefault(
            table_family, {"seen": 0, "processed": 0, "deferred": 0, "rejected": 0}
        )

    def admit(
        self, record: Mapping[str, Any], *, table_family: str, org_id: str
    ) -> SecOpsAdmission:
        """Admit one SecOps record: fold within budget, defer-and-count past it."""
        if table_family not in _WORKFLOW_KEYS:
            raise ValueError(f"unknown SecOps table family {table_family!r}")
        self._seen += 1
        counts = self._table_counts(table_family)
        counts["seen"] += 1

        sys_id = _text(record.get("sys_id"))
        rec_org = _text(record.get("org_id"))
        if rec_org and rec_org != org_id:
            raise SecOpsOrgScopeError(
                f"record belongs to org {rec_org!r}, cannot admit under {org_id!r}"
            )
        if not sys_id:
            self._rejected += 1
            counts["rejected"] += 1
            return SecOpsAdmission(None, "rejected")

        cursor = _text(record.get("sys_updated_on") or record.get("source_timestamp"))
        dt = _parse_dt(cursor)

        # Per-run budget: once the budgeted window is full, defer-and-count — the
        # safe checkpoint is NOT advanced, so deferred work is re-fetched next run.
        if not self._budget.has_capacity():
            self._budget.defer(f"servicenow:{table_family}", cursor, dt)
            counts["deferred"] += 1
            return SecOpsAdmission(None, "deferred")

        self._budget.charge()
        counts["processed"] += 1
        fold_key, workflow = _WORKFLOW_KEYS[table_family](record, self._cmdb_index)
        agg_key = (org_id, table_family, fold_key)
        aggregate = self._aggregates.get(agg_key)
        disposition = "folded"
        if aggregate is None:
            aggregate = WorkflowAggregate(
                table_family=table_family, org_id=org_id, fold_key=fold_key, workflow=workflow
            )
            self._aggregates[agg_key] = aggregate
            disposition = "new"
        aggregate._fold(record, dt, cursor)
        self._advance_checkpoint(table_family, cursor, dt)
        return SecOpsAdmission(aggregate, disposition)

    def _advance_checkpoint(
        self, table_family: str, cursor: Optional[str], dt: Optional[datetime]
    ) -> None:
        if cursor is None or dt is None:
            # A processed record with no parseable cursor cannot move the resume
            # watermark; keep the last good one (never regress the checkpoint).
            self._safe_checkpoints.setdefault(table_family, self._safe_checkpoints.get(table_family))
            return
        current = self._safe_dt.get(table_family)
        if current is None or dt > current:
            self._safe_dt[table_family] = dt
            self._safe_checkpoints[table_family] = cursor

    # -- read side -----------------------------------------------------------

    def workflow_aggregates(self, org_id: Optional[str] = None) -> List[WorkflowAggregate]:
        """Detector-visible workflow aggregates, deterministically ordered."""
        aggregates = [
            a for a in self._aggregates.values() if org_id is None or a.org_id == org_id
        ]
        aggregates.sort(key=lambda a: (a.table_family, a.fold_key))
        return aggregates

    def budget_report(self) -> BudgetReport:
        """The reused MSP-B7 budget outcome (loud-deferral proof)."""
        return self._budget.snapshot()

    def measurements(self, org_id: Optional[str] = None) -> SecOpsVolumeMeasurements:
        """Assemble the full VR volume measurement (run-health / calibration hand-off)."""
        aggregates = self.workflow_aggregates(org_id)
        return SecOpsVolumeMeasurements(
            budget=self._budget_limit,
            records_seen=self._seen,
            records_processed=self._budget.processed,
            records_deferred=self._budget.deferred,
            records_rejected=self._rejected,
            aggregate_count=len(aggregates),
            per_table={k: dict(v) for k, v in self._per_table.items()},
            safe_checkpoints=dict(self._safe_checkpoints),
            budget_report=self._budget.snapshot().to_dict(),
            workflow_aggregates=[a.to_dict() for a in aggregates],
        )
