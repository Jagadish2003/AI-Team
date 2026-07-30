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


def _entity_keys_as_of_run(org_id: str, run_id: str) -> Optional[List[str]]:
    """The resolved entity population visible as of one run.

    Reuses ``ENTITIES_VISIBLE_AS_OF_RUN_FROM_WHERE`` — the shared clause both
    existing chronological-visibility paths bind, documented there as the thing
    that must not drift. Returns ``None`` (not ``[]``) when the population cannot
    be read: not knowing is different from knowing it is empty, and only ``None``
    correctly suppresses a fabricated population confounder.
    """
    try:
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


def _projected_horizon_days(
    org_id: str, opportunity_identity: str
) -> Optional[int]:
    """A1's projected observation horizon, from the stored projection.

    Read from the projection history (A1 T6) rather than recomputed, so the
    horizon judged against is the one that was actually projected.
    """
    try:
        from .projection_store import get_projection_history

        history = get_projection_history(opportunity_identity, org_id=org_id)
    except Exception:  # noqa: BLE001
        return None
    for entry in history:
        horizon = (entry.get("projection") or {}).get("observationHorizonDays")
        if isinstance(horizon, int):
            return horizon
    return None


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
        projected_horizon_days=_projected_horizon_days(org_id, opportunity_identity),
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
                org_id, str(record.get("baselineRunId") or "")
            ),
            current_entity_keys=_entity_keys_as_of_run(
                org_id, str(record.get("currentRunId") or "")
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
    primary = next(
        (m for m in record.get("movements") or [] if m.get("role") == "movement"),
        None,
    ) or next(iter(record.get("movements") or []), None) or {}
    now = _now()
    comparability = record.get("comparability") or {}
    summary = record.get("confounderSummary") or {}

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_movements ("
                "  org_id, opportunity_identity, current_run_id, baseline_run_id,"
                "  detector_id, action_date, comparability_verdict,"
                "  baseline_pack_version, current_pack_version, primary_signal,"
                "  primary_baseline_value, primary_current_value, primary_delta,"
                "  primary_direction, record, measured_at, created_at, updated_at,"
                "  confounder_count, confounder_material_count, confounder_types"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s) "
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
                ),
            )
        con.commit()


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
    return _row_to_record(row) if row else None


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
    return [_row_to_record(row) for row in rows]


def get_movements_for_run(org_id: str, run_id: str) -> List[Dict[str, Any]]:
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s AND current_run_id = %s "
                "ORDER BY opportunity_identity ASC",
                (org_id, run_id),
            )
            rows = cur.fetchall()
    return [_row_to_record(row) for row in rows]


def list_movements(
    org_id: str, *, verdicts: Optional[Sequence[str]] = None, limit: int = 200
) -> List[Dict[str, Any]]:
    """Every stored measurement in one org, newest first.

    Filterable by comparability verdict, which is what lets T6's portfolio view
    count caveated measurements rather than averaging them away.
    """
    sql = _SELECT + " WHERE org_id = %s"
    params: List[Any] = [org_id]
    if verdicts:
        sql += f" AND comparability_verdict IN ({', '.join(['%s'] * len(verdicts))})"
        params.extend(verdicts)
    sql += " ORDER BY measured_at DESC LIMIT %s"
    params.append(max(1, min(int(limit), 1000)))

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return [_row_to_record(row) for row in rows]


__all__ = [
    "MOVEMENT_SCHEMA_VERSION",
    "SKIP_NOT_ACTIONED",
    "SKIP_NO_ACTION_DATE",
    "SKIP_NO_BASELINE",
    "SKIP_NO_CURRENT_SIGNALS",
    "SKIP_NO_POST_ACTION_RUN",
    "MovementMeasurementSkipped",
    "ensure_opportunity_movement_table",
    "get_movement",
    "get_movement_history",
    "get_movements_for_run",
    "list_movements",
    "measure_movement",
    "measure_movements_for_run",
]
