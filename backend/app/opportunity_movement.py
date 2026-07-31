"""2.0-A2 T3 — post-action monitoring: measure movement and store the record.

The mechanical heart of A2. For an opportunity a human marked ``actioned``,
re-measure the same signals its T2 baseline froze, over a comparable window, as
subsequent runs land — and store the comparison.

**Re-uses the existing retrieval rather than adding a parallel matching path.**
``opportunity_identity`` is stable across runs by construction, so "the same
finding, later" is a lookup: ``get_instances_by_identity`` already returns the
cross-run series oldest-first. The re-measured signal values come from
``temporal.get_run_signal_rows`` — the per-run signal snapshots the platform
already writes. Nothing here re-implements matching or re-derives a signal.

**Three gates, all of which must pass before a measurement exists:**

1. the lifecycle state must be one a recorded action put it in (T1's
   ``is_measurable``) — no outcome without action;
2. a frozen baseline must exist (T2) — nothing to compare against otherwise;
3. at least one run must have landed STRICTLY AFTER the action date.

Fail any gate and the answer is **no record** — never a zero-delta one. "We have
not measured" and "we measured no change" are different facts, and conflating
them manufactures a false reassurance.

**Attribution discipline.** This module measures movement and records the
comparison. It never claims credit, and the record's vocabulary is
movement-and-comparison shaped throughout.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import db
from .opportunity_movement_record import (
    MOVEMENT_SCHEMA_VERSION,
    build_movement_record,
)
from .outcome_confounders import detect_confounders, summarise_confounders
from .projection_validation import (
    build_projection_validation,
    select_projection_entry_for_baseline,
    validation_filter_values,
)
from database.models.opportunity_movements import ALL_OPPORTUNITY_MOVEMENTS_DDL

logger = logging.getLogger(__name__)

_TABLE_READY = False


class MovementMeasurementSkipped(Exception):
    """No measurement is possible, with the reason why.

    Raised rather than returning ``None`` silently so a caller can report WHICH
    gate stopped it — the difference between "not actioned", "no baseline" and
    "no post-action run yet" matters to an analyst looking at an empty outcome
    view.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


# Skip reasons — stable strings; T6's portfolio view explains empty states with them.
SKIP_NOT_ACTIONED = "not_actioned"
SKIP_NO_ACTION_DATE = "no_action_date"
SKIP_NO_BASELINE = "no_baseline"
SKIP_NO_POST_ACTION_RUN = "no_post_action_run"
SKIP_NO_CURRENT_SIGNALS = "no_current_signal_values"
SKIP_ACTION_DATE_MISMATCH = "action_date_mismatch"

ACTION_VALIDITY_VALID = "valid"
ACTION_VALIDITY_INVALIDATED = "invalidated"
ACTION_INVALIDATED_REASON_REVERSED = "action_reversed"


def ensure_opportunity_movement_table() -> None:
    """Create the movement table if absent (idempotent, never raises)."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                    ("opportunity_movements",),
                )
                if cur.fetchone() is None:
                    for ddl in ALL_OPPORTUNITY_MOVEMENTS_DDL:
                        cur.execute(ddl)
            con.commit()
        _TABLE_READY = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ensure_opportunity_movement_table skipped (assuming provisioned): %s",
            exc,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = _parse_dt(value)
    return parsed.date() if parsed else None


def _require_current_recorded_action(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a storeable copy only when today's lifecycle still has an action.

    T7 makes this the durable invariant for measurement generation, backfills,
    replay helpers, and any future admin/debug write path: a movement row cannot
    be persisted unless the current lifecycle row carries the same
    customer-recorded action date the measurement says it followed.
    """
    from .opportunity_lifecycle import get_lifecycle
    from .opportunity_lifecycle_states import is_measurable

    org_id = str(record.get("orgId") or "").strip()
    opportunity_identity = str(record.get("opportunityIdentity") or "").strip()
    if not org_id or not opportunity_identity:
        raise MovementMeasurementSkipped(
            SKIP_NOT_ACTIONED,
            "movement record is missing orgId or opportunityIdentity, so a "
            "recorded action cannot be verified",
        )

    lifecycle = get_lifecycle(org_id, opportunity_identity)
    state = str((lifecycle or {}).get("state") or "untracked")
    if lifecycle is None or not is_measurable(state):
        raise MovementMeasurementSkipped(
            SKIP_NOT_ACTIONED,
            f"opportunity {opportunity_identity!r} cannot receive an outcome "
            f"measurement without a current recorded action (state={state!r})",
        )

    lifecycle_action_date = _parse_date(lifecycle.get("actionDate"))
    record_action_date = _parse_date(record.get("actionDate"))
    if lifecycle_action_date is None or record_action_date is None:
        raise MovementMeasurementSkipped(
            SKIP_NO_ACTION_DATE,
            f"opportunity {opportunity_identity!r} has no action date to anchor "
            "the movement write on",
        )
    if lifecycle_action_date != record_action_date:
        raise MovementMeasurementSkipped(
            SKIP_ACTION_DATE_MISMATCH,
            f"movement for opportunity {opportunity_identity!r} was measured "
            f"against action date {record_action_date.isoformat()}, but the "
            f"current recorded action date is {lifecycle_action_date.isoformat()}",
        )

    checked_at = _now()
    out = dict(record)
    out["actionValidity"] = {
        "state": ACTION_VALIDITY_VALID,
        "actionDate": lifecycle_action_date.isoformat(),
        "lifecycleState": state,
        "lifecycleRevision": int(lifecycle.get("revision") or 0),
        "checkedAt": checked_at.isoformat(),
    }
    return out


def _record_depends_on_current_action(org_id: str, record: Mapping[str, Any]) -> bool:
    """Read-side guard: stale or invalidated rows are not outcome artifacts."""
    validity = record.get("actionValidity")
    if (
        isinstance(validity, Mapping)
        and validity.get("state") == ACTION_VALIDITY_INVALIDATED
    ):
        return False

    opportunity_identity = str(record.get("opportunityIdentity") or "").strip()
    if not opportunity_identity:
        return False

    try:
        from .opportunity_lifecycle import get_lifecycle

        lifecycle = get_lifecycle(org_id, opportunity_identity)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not verify action for stored movement %s: %s",
            opportunity_identity,
            exc,
        )
        return False

    lifecycle_action_date = _parse_date((lifecycle or {}).get("actionDate"))
    record_action_date = _parse_date(record.get("actionDate"))
    return bool(
        lifecycle
        and lifecycle_action_date is not None
        and record_action_date is not None
        and lifecycle_action_date == record_action_date
    )


def _visible_records(org_id: str, rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    records = [_row_to_record(row) for row in rows]
    return [
        record
        for record in records
        if record and _record_depends_on_current_action(org_id, record)
    ]


# --------------------------------------------------------------------------
# Reading the current side
# --------------------------------------------------------------------------


def _current_signal_values(
    org_id: str, run_id: str, detector_id: str
) -> Tuple[Dict[str, float], Optional[datetime], Optional[int]]:
    """Re-measure this detector's signals from one run's stored snapshots.

    Returns ``(values_by_metric_name, captured_at, window_days)``. Reads the
    snapshots the platform already writes — there is no second measurement path.
    """
    from .temporal import get_run_signal_rows

    values: Dict[str, float] = {}
    captured_at: Optional[datetime] = None
    window_days: Optional[int] = None

    for row in get_run_signal_rows(org_id, run_id) or []:
        if str(row.get("detector_id") or "") != detector_id:
            continue
        name = str(row.get("metric_name") or "").strip()
        raw = row.get("metric_value")
        if name and raw is not None:
            try:
                values[name] = float(raw)
            except (TypeError, ValueError):
                continue
        row_captured = _parse_dt(row.get("captured_at"))
        if row_captured and (captured_at is None or row_captured > captured_at):
            captured_at = row_captured
        if window_days is None and row.get("baseline_window_days"):
            try:
                window_days = int(row["baseline_window_days"])
            except (TypeError, ValueError):
                pass

    return values, captured_at, window_days


def _entity_keys_as_of_run(
    org_id: str,
    run_id: str,
    *,
    opportunity_identity: Optional[str] = None,
) -> Optional[List[str]]:
    """The resolved entity population visible as of one run.

    Reuses ``ENTITIES_VISIBLE_AS_OF_RUN_FROM_WHERE`` — the shared clause both
    existing chronological-visibility paths bind, documented there as the thing
    that must not drift. Returns ``None`` (not ``[]``) when the population cannot
    be read: not knowing is different from knowing it is empty, and only ``None``
    correctly suppresses a fabricated population confounder.
    """
    try:
        if opportunity_identity:
            by_instance_time = _entity_keys_as_of_run_for_opportunity(
                org_id,
                run_id,
                opportunity_identity,
            )
            if by_instance_time is not None:
                return by_instance_time

        from database.models.entities import ENTITIES_VISIBLE_AS_OF_RUN_FROM_WHERE

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT e.entity_type, e.canonical_name "
                    + ENTITIES_VISIBLE_AS_OF_RUN_FROM_WHERE,
                    (org_id, run_id),
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not read the entity population as of run %s: %s", run_id, exc
        )
        return None
    # The stable resolution key, not the random per-row uuid — the same identity
    # entity_resolution dedupes on, so a recreated row is not read as a change.
    return sorted(f"{row[0]}:{row[1]}" for row in rows)


def _entity_keys_as_of_run_for_opportunity(
    org_id: str,
    run_id: str,
    opportunity_identity: str,
) -> Optional[List[str]]:
    """Resolved entity population using this opportunity's observed run order.

    The shared entity visibility clause derives chronology from ``runs.seq``.
    Movement records also have the stable ``opportunity_identity`` and the
    opportunity instance series, so use that series' timestamps when available.
    This keeps a reused or replayed run id from making a later observation look
    older than the baseline and fabricating a CI population removal.
    """
    identity = str(opportunity_identity or "").strip()
    if not identity:
        return None

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT run_id, created_at FROM opportunity_instances "
                "WHERE org_id = %s AND opportunity_identity = %s "
                "AND is_deleted = FALSE",
                (org_id, identity),
            )
            series_rows = cur.fetchall()
            run_times: Dict[str, datetime] = {}
            for row in series_rows:
                parsed = _parse_dt(row[1])
                if row[0] and parsed is not None:
                    run_times[str(row[0])] = parsed
            target_created = run_times.get(run_id)
            if target_created is None:
                return None

            cur.execute(
                "SELECT entity_type, canonical_name, first_seen_run_id "
                "FROM entities WHERE org_id = %s",
                (org_id,),
            )
            entity_rows = cur.fetchall()

            unknown_run_ids = sorted(
                {
                    str(row[2])
                    for row in entity_rows
                    if row[2] and str(row[2]) not in run_times
                }
                | {run_id}
            )
            seq_by_run: Dict[str, int] = {}
            if unknown_run_ids:
                placeholders = ", ".join(["%s"] * len(unknown_run_ids))
                cur.execute(
                    f"SELECT id, seq FROM runs WHERE id IN ({placeholders})",
                    tuple(unknown_run_ids),
                )
                seq_by_run = {str(row[0]): int(row[1]) for row in cur.fetchall()}

    target_seq = seq_by_run.get(run_id)
    keys: List[str] = []
    for entity_type, canonical_name, first_seen_run_id in entity_rows:
        first_seen = str(first_seen_run_id or "")
        first_seen_at = run_times.get(first_seen)
        if first_seen_at is not None:
            if first_seen_at <= target_created:
                keys.append(f"{entity_type}:{canonical_name}")
            continue

        first_seen_seq = seq_by_run.get(first_seen)
        if first_seen_seq is None or (
            target_seq is not None and first_seen_seq <= target_seq
        ):
            keys.append(f"{entity_type}:{canonical_name}")

    return sorted(keys)


def _lower_is_better_map(detector_id: str) -> Dict[str, bool]:
    """Which direction is an improvement, per signal, from the A1 registry.

    Without this every delta would be reported as if smaller were always better,
    which is wrong for a coverage-style signal.
    """
    try:
        from discovery.projection.signal_registry import get_detector_profile
    except Exception:  # noqa: BLE001
        return {}
    profile = get_detector_profile(detector_id)
    if profile is None:
        return {}
    lower = bool(profile.lower_is_better)
    names = [profile.movement_signal, profile.instance_field, profile.volume_signal]
    return {name: lower for name in names if name}


def _projection_entry_for_baseline(
    org_id: str, opportunity_identity: str, baseline_run_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    """The A1 projection stored on the baseline run, never a later projection."""
    try:
        from .projection_store import get_projection_history

        history = get_projection_history(opportunity_identity, org_id=org_id)
    except Exception:  # noqa: BLE001
        return None
    return select_projection_entry_for_baseline(baseline_run_id, history)


def _projection_horizon_days(
    projection: Optional[Mapping[str, Any]]
) -> Optional[int]:
    """A1's stored observation horizon, when structurally usable."""
    if not isinstance(projection, Mapping):
        return None
    try:
        value = int(projection.get("observationHorizonDays"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


def measure_movement(
    org_id: str,
    opportunity_identity: str,
    current_run_id: str,
    *,
    measured_at: Optional[datetime] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Measure one opportunity's movement against its frozen baseline.

    Raises :class:`MovementMeasurementSkipped` when any gate fails — the caller
    learns which one rather than receiving an empty record.
    """
    from .opportunity_baseline import get_baseline
    from .opportunity_lifecycle import get_lifecycle
    from .opportunity_lifecycle_states import is_measurable

    now = measured_at or _now()

    # Gate 1 — no outcome without a recorded action.
    lifecycle = get_lifecycle(org_id, opportunity_identity)
    if lifecycle is None or not is_measurable(lifecycle["state"]):
        raise MovementMeasurementSkipped(
            SKIP_NOT_ACTIONED,
            f"opportunity {opportunity_identity!r} is not in a state a recorded "
            f"action put it in (state="
            f"{lifecycle['state'] if lifecycle else 'untracked'!r})",
        )
    action_date = _parse_date(lifecycle.get("actionDate"))
    if action_date is None:
        # Defensive: T1 makes this unreachable, since actioned requires a date.
        raise MovementMeasurementSkipped(
            SKIP_NO_ACTION_DATE,
            f"opportunity {opportunity_identity!r} has no action date to anchor "
            "the post-action window on",
        )

    # Gate 2 — nothing to compare against without a frozen baseline.
    baseline = get_baseline(org_id, opportunity_identity)
    if baseline is None:
        raise MovementMeasurementSkipped(
            SKIP_NO_BASELINE,
            f"opportunity {opportunity_identity!r} has no frozen baseline; a "
            "finding created before baseline capture shipped is not measurable",
        )

    detector_id = str(baseline.get("detectorId") or "").strip()
    projection_entry = _projection_entry_for_baseline(
        org_id, opportunity_identity, baseline.get("runId")
    )
    stored_projection = (
        projection_entry.get("projection")
        if isinstance(projection_entry, Mapping)
        else None
    )

    # Gate 3 — at least one run strictly after the action date. Anchored on the
    # action date only: there is no "most recent run" fallback, because that
    # would silently fold pre-action observations into the comparison.
    post_action = _post_action_instances(org_id, opportunity_identity, action_date)
    if not post_action:
        raise MovementMeasurementSkipped(
            SKIP_NO_POST_ACTION_RUN,
            f"no run has landed strictly after the action date "
            f"{action_date.isoformat()} for opportunity {opportunity_identity!r}",
        )

    post_action_run_ids = [i["runId"] for i in post_action]
    post_action_run_dates = [i["createdAt"] for i in post_action if i["createdAt"]]

    if current_run_id not in post_action_run_ids:
        raise MovementMeasurementSkipped(
            SKIP_NO_POST_ACTION_RUN,
            f"run {current_run_id!r} did not land after the action date "
            f"{action_date.isoformat()}, so it cannot be a post-action measurement",
        )

    current_values, current_captured, current_window_days = _current_signal_values(
        org_id, current_run_id, detector_id
    )
    if not current_values:
        raise MovementMeasurementSkipped(
            SKIP_NO_CURRENT_SIGNALS,
            f"run {current_run_id!r} recorded no signal values for detector "
            f"{detector_id!r}, so there is nothing to re-measure",
        )

    current_pack_version = next(
        (i["packVersion"] for i in post_action if i["runId"] == current_run_id), None
    )
    window_days = current_window_days or (baseline.get("window") or {}).get("days")
    window_end = current_captured or now
    window_start = (
        window_end - _timedelta_days(window_days) if window_days else None
    )

    record = build_movement_record(
        org_id=org_id,
        opportunity_identity=opportunity_identity,
        detector_id=detector_id,
        action_date=action_date,
        baseline_artifact=baseline,
        current_run_id=current_run_id,
        current_values=current_values,
        current_window_days=window_days,
        current_window_start=window_start,
        current_window_end=window_end,
        current_pack_version=current_pack_version,
        post_action_run_ids=post_action_run_ids,
        post_action_run_dates=post_action_run_dates,
        projected_horizon_days=_projection_horizon_days(stored_projection),
        measured_at=now,
        lower_is_better_by_signal=_lower_is_better_map(detector_id),
    )

    # 2.0-A2 T4 — attach labelled confounder caveats. Detection APPENDS caveats:
    # it never adjusts the delta and never blocks the measurement, so a detector
    # failure or a detected confounder both leave the record publishable.
    record["confounders"] = _detect_record_confounders(
        org_id, opportunity_identity, baseline, record
    )
    record["confounderSummary"] = summarise_confounders(record["confounders"])
    record["projectionValidation"] = build_projection_validation(
        record,
        stored_projection,
        projection_entry=projection_entry,
    )

    if persist:
        _store_movement(record)
    return record


def _detect_record_confounders(
    org_id: str,
    opportunity_identity: str,
    baseline: Mapping[str, Any],
    record: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Run confounder detection for one record. Never raises.

    A failure here must not cost the measurement — but it must also not be
    invisible, because a result published with fewer caveats than were detectable
    is the exact failure this subtask exists to prevent.
    """
    try:
        return detect_confounders(
            org_id=org_id,
            opportunity_identity=opportunity_identity,
            baseline=baseline,
            movement=record,
            baseline_entity_keys=_entity_keys_as_of_run(
                org_id,
                str(record.get("baselineRunId") or ""),
                opportunity_identity=opportunity_identity,
            ),
            current_entity_keys=_entity_keys_as_of_run(
                org_id,
                str(record.get("currentRunId") or ""),
                opportunity_identity=opportunity_identity,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Confounder detection failed for %s (measurement still reports): %s",
            opportunity_identity,
            exc,
        )
        return []


def _timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=int(days))


def _post_action_instances(
    org_id: str, opportunity_identity: str, action_date: date
) -> List[Dict[str, Any]]:
    """Instances observed STRICTLY after the action date, oldest first.

    Reuses ``get_instances_by_identity`` — the existing cross-run series — rather
    than introducing a parallel matching path.
    """
    from .opportunity_instances import get_instances_by_identity

    out: List[Dict[str, Any]] = []
    for instance in get_instances_by_identity(opportunity_identity, org_id=org_id):
        created = instance.created_at
        if created is None:
            continue
        created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        # STRICTLY after: an observation on the action date itself may predate the
        # change within that day, so it is not counted as post-action.
        if created.date() <= action_date:
            continue
        out.append(
            {
                "runId": instance.run_id,
                "createdAt": created,
                "packVersion": instance.pack_version,
            }
        )
    return out


def _store_movement(record: Mapping[str, Any]) -> None:
    """Persist one movement record, idempotent per (identity, comparison run).

    DO UPDATE rather than DO NOTHING: re-measuring the SAME run pair should
    correct itself rather than duplicate. That is not revisionism — the baseline
    (T2) is the write-once record of what we were judged against; this is a
    derived measurement of one specific run pair, and re-deriving it for that same
    pair is idempotent by definition.
    """
    record = _require_current_recorded_action(record)
    primary = next(
        (m for m in record.get("movements") or [] if m.get("role") == "movement"),
        None,
    ) or next(iter(record.get("movements") or []), None) or {}
    now = _now()
    comparability = record.get("comparability") or {}
    summary = record.get("confounderSummary") or {}
    validation = validation_filter_values(record.get("projectionValidation"))

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_movements ("
                "  org_id, opportunity_identity, current_run_id, baseline_run_id,"
                "  detector_id, action_date, comparability_verdict,"
                "  baseline_pack_version, current_pack_version, primary_signal,"
                "  primary_baseline_value, primary_current_value, primary_delta,"
                "  primary_direction, record, measured_at, created_at, updated_at,"
                "  confounder_count, confounder_material_count, confounder_types,"
                "  projection_validation_verdict, projection_pack_id,"
                "  projection_pack_version, projection_confidence"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (org_id, opportunity_identity, current_run_id) "
                "DO UPDATE SET "
                "  baseline_run_id = EXCLUDED.baseline_run_id,"
                "  comparability_verdict = EXCLUDED.comparability_verdict,"
                "  current_pack_version = EXCLUDED.current_pack_version,"
                "  primary_signal = EXCLUDED.primary_signal,"
                "  primary_baseline_value = EXCLUDED.primary_baseline_value,"
                "  primary_current_value = EXCLUDED.primary_current_value,"
                "  primary_delta = EXCLUDED.primary_delta,"
                "  primary_direction = EXCLUDED.primary_direction,"
                "  record = EXCLUDED.record,"
                "  measured_at = EXCLUDED.measured_at,"
                "  confounder_count = EXCLUDED.confounder_count,"
                "  confounder_material_count = EXCLUDED.confounder_material_count,"
                "  confounder_types = EXCLUDED.confounder_types,"
                "  projection_validation_verdict = EXCLUDED.projection_validation_verdict,"
                "  projection_pack_id = EXCLUDED.projection_pack_id,"
                "  projection_pack_version = EXCLUDED.projection_pack_version,"
                "  projection_confidence = EXCLUDED.projection_confidence,"
                "  updated_at = EXCLUDED.updated_at",
                (
                    record["orgId"],
                    record["opportunityIdentity"],
                    record["currentRunId"],
                    record["baselineRunId"],
                    record["detectorId"],
                    _parse_date(record["actionDate"]),
                    comparability.get("verdict"),
                    (record.get("baseline") or {}).get("packVersion"),
                    (record.get("current") or {}).get("packVersion"),
                    primary.get("signalName"),
                    primary.get("baselineValue"),
                    primary.get("currentValue"),
                    primary.get("delta"),
                    primary.get("direction"),
                    json.dumps(record),
                    _parse_dt(record["measuredAt"]),
                    now,
                    now,
                    int(summary.get("count", 0)),
                    int(summary.get("materialCount", 0)),
                    json.dumps(summary.get("types", [])),
                    validation["verdict"],
                    validation["packId"],
                    validation["packVersion"],
                    validation["confidence"],
                ),
            )
        con.commit()


def invalidate_movements_for_action_reversal(
    org_id: str,
    opportunity_identity: str,
    *,
    reason: str = ACTION_INVALIDATED_REASON_REVERSED,
    invalidated_by: Optional[str] = None,
) -> int:
    """Mark stored movement rows as invalid when their recorded action is unwound.

    Rows are retained for audit/debug traceability, but read paths hide them once
    marked. That keeps T7's invariant visible in the data without letting an old
    movement continue to appear as an outcome after its action date was cleared.
    """
    ensure_opportunity_movement_table()
    invalidated_at = _now()
    updated = 0
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT current_run_id, record FROM opportunity_movements "
                "WHERE org_id = %s AND opportunity_identity = %s",
                (org_id, opportunity_identity),
            )
            rows = cur.fetchall()
            for current_run_id, raw_record in rows:
                try:
                    record = json.loads(raw_record) if raw_record else {}
                except (TypeError, ValueError):
                    record = {}
                if not isinstance(record, dict):
                    record = {}
                previous = record.get("actionValidity")
                marker: Dict[str, Any] = {
                    "state": ACTION_VALIDITY_INVALIDATED,
                    "reason": reason,
                    "invalidatedAt": invalidated_at.isoformat(),
                    "invalidatedBy": invalidated_by,
                    "actionDate": record.get("actionDate"),
                }
                if isinstance(previous, Mapping):
                    marker["previous"] = dict(previous)
                record["actionValidity"] = marker
                cur.execute(
                    "UPDATE opportunity_movements SET record = %s, updated_at = %s "
                    "WHERE org_id = %s AND opportunity_identity = %s "
                    "AND current_run_id = %s",
                    (
                        json.dumps(record),
                        invalidated_at,
                        org_id,
                        opportunity_identity,
                        current_run_id,
                    ),
                )
                updated += cur.rowcount
        con.commit()
    return updated


def measure_movements_for_run(
    org_id: str, run_id: str
) -> Dict[str, Any]:
    """Measure every actioned opportunity this run could serve as a current side.

    Non-blocking: a per-opportunity failure is counted and logged, never fatal.
    Returns counts plus the skip reasons, so an empty result is explainable
    instead of mysterious.
    """
    from .opportunity_instances import get_instances_for_run

    ensure_opportunity_movement_table()
    counts = {"measured": 0, "skipped": 0, "failed": 0}
    skips: Dict[str, int] = {}
    measured_ids: List[str] = []

    identities = {
        instance.opportunity_identity
        for instance in get_instances_for_run(run_id, org_id=org_id)
        if instance.opportunity_identity
    }

    for identity in sorted(identities):
        try:
            measure_movement(org_id, identity, run_id)
        except MovementMeasurementSkipped as exc:
            counts["skipped"] += 1
            skips[exc.reason] = skips.get(exc.reason, 0) + 1
            continue
        except Exception as exc:  # noqa: BLE001 - never break a run
            counts["failed"] += 1
            logger.warning(
                "Movement measurement failed for %s in run %s (non-blocking): %s",
                identity,
                run_id,
                exc,
            )
            continue
        counts["measured"] += 1
        measured_ids.append(identity)

    return {**counts, "skipReasons": skips, "measuredIdentities": measured_ids}


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

_SELECT = (
    "SELECT org_id, opportunity_identity, current_run_id, baseline_run_id, "
    "detector_id, action_date, comparability_verdict, baseline_pack_version, "
    "current_pack_version, primary_signal, primary_baseline_value, "
    "primary_current_value, primary_delta, primary_direction, record, "
    "measured_at, created_at, updated_at FROM opportunity_movements"
)


def _row_to_record(row: Sequence[Any]) -> Dict[str, Any]:
    try:
        record = json.loads(row[14]) if row[14] else {}
    except (TypeError, ValueError):
        logger.warning("Stored movement record for %s is not valid JSON", row[1])
        record = {}
    return record if isinstance(record, dict) else {}


def get_movement(
    org_id: str, opportunity_identity: str, current_run_id: str
) -> Optional[Dict[str, Any]]:
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s AND opportunity_identity = %s "
                "AND current_run_id = %s",
                (org_id, opportunity_identity, current_run_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    record = _row_to_record(row)
    return record if _record_depends_on_current_action(org_id, record) else None


def get_movement_history(
    org_id: str, opportunity_identity: str, *, limit: int = 200
) -> List[Dict[str, Any]]:
    """Every measurement for one opportunity, oldest first — the outcome series."""
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s AND opportunity_identity = %s "
                "ORDER BY measured_at ASC LIMIT %s",
                (org_id, opportunity_identity, max(1, min(int(limit), 1000))),
            )
            rows = cur.fetchall()
    return _visible_records(org_id, rows)


def get_movements_for_run(org_id: str, run_id: str) -> List[Dict[str, Any]]:
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s AND current_run_id = %s "
                "ORDER BY opportunity_identity ASC",
                (org_id, run_id),
            )
            rows = cur.fetchall()
    return _visible_records(org_id, rows)


def list_movements(
    org_id: str,
    *,
    verdicts: Optional[Sequence[str]] = None,
    projection_verdicts: Optional[Sequence[str]] = None,
    pack_ids: Optional[Sequence[str]] = None,
    detector_ids: Optional[Sequence[str]] = None,
    confidences: Optional[Sequence[str]] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Every stored measurement in one org, newest first.

    Filterable by comparability verdict, which is what lets T6's portfolio view
    count caveated measurements rather than averaging them away. Also filterable
    by T5's projection-validation verdict, pack, detector and confidence so A1
    and A3 can consume calibration data without scraping per-record JSON.
    """
    sql = _SELECT + " WHERE org_id = %s"
    params: List[Any] = [org_id]
    if verdicts:
        sql += f" AND comparability_verdict IN ({', '.join(['%s'] * len(verdicts))})"
        params.extend(verdicts)
    if projection_verdicts:
        sql += (
            " AND projection_validation_verdict IN "
            f"({', '.join(['%s'] * len(projection_verdicts))})"
        )
        params.extend(projection_verdicts)
    if pack_ids:
        sql += f" AND projection_pack_id IN ({', '.join(['%s'] * len(pack_ids))})"
        params.extend(pack_ids)
    if detector_ids:
        sql += f" AND detector_id IN ({', '.join(['%s'] * len(detector_ids))})"
        params.extend(detector_ids)
    if confidences:
        sql += (
            " AND UPPER(projection_confidence) IN "
            f"({', '.join(['%s'] * len(confidences))})"
        )
        params.extend(str(c).upper() for c in confidences)
    sql += " ORDER BY measured_at DESC LIMIT %s"
    params.append(max(1, min(int(limit), 1000)))

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return _visible_records(org_id, rows)


__all__ = [
    "MOVEMENT_SCHEMA_VERSION",
    "SKIP_NOT_ACTIONED",
    "SKIP_NO_ACTION_DATE",
    "SKIP_NO_BASELINE",
    "SKIP_NO_CURRENT_SIGNALS",
    "SKIP_NO_POST_ACTION_RUN",
    "SKIP_ACTION_DATE_MISMATCH",
    "ACTION_INVALIDATED_REASON_REVERSED",
    "MovementMeasurementSkipped",
    "ensure_opportunity_movement_table",
    "get_movement",
    "get_movement_history",
    "get_movements_for_run",
    "invalidate_movements_for_action_reversal",
    "list_movements",
    "measure_movement",
    "measure_movements_for_run",
]
