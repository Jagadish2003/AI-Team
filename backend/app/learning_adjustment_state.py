"""2.0-A3 T2 — reading and writing the stored per-org adjustment state.

The state is a VALUE, computed deliberately from T1's signal set and written
here, not an expression evaluated at read time. See
``database/models/ranking_adjustments.py`` for why that distinction is what makes
T4's audit and reset answerable.

**Recomputation is explicit.** Nothing here runs on the serving path: serving
READS the stored value. A ranking that shifted because someone opened a page
would be exactly the invisible drift A3 exists to prevent.

**Cold start is stored, not inferred.** When T1's signal set is inactive the
recomputation still writes rows, with ``learning_active = FALSE`` and a zero
weight. A zero that means "not enough evidence yet" and a zero that means
"learning weighed this and arrived at neutral" are different facts, and a reader
who cannot tell them apart will misread the first as the second.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import db
from .learning_adjustment import GroupAdjustment
from .learning_signal_config import load_config
from .ranking_adjustment_audit import emit_ranking_adjustment_changed

logger = logging.getLogger(__name__)

ADJUSTMENT_STATE_SCHEMA_VERSION = "1.0.0"

CHANGE_RECOMPUTED = "recomputed"
CHANGE_ACTIVATED = "activated"
CHANGE_DEACTIVATED = "deactivated"
CHANGE_RESET = "reset"

ACTOR_SYSTEM = "system"

#: The sentinel a NULL group dimension is stored as. A NULL inside a primary key
#: would let duplicate rows accumulate for the same real group, so the empty
#: string carries "unknown" and is translated back to None on read.
_UNKNOWN = ""


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _key_part(value: Optional[str]) -> str:
    return _clean(value).lower() or _UNKNOWN


def _from_key_part(value: Any) -> Optional[str]:
    text = _clean(value).lower()
    return text or None


def _refs(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(r) for r in value if isinstance(r, Mapping)]


def _affected_opportunity_count(rows: Sequence[Mapping[str, Any]]) -> int:
    """Distinct identities whose signals fed the active state being changed."""
    identities = set()
    for row in rows:
        if not row.get("learningActive") or not float(row.get("netWeight") or 0.0):
            continue
        for ref in _refs(row.get("contributingRefs")):
            identity = _clean(ref.get("opportunityIdentity"))
            if identity:
                identities.add(identity)
    return len(identities)


def _state_has_active(rows: Sequence[Mapping[str, Any]]) -> bool:
    return any(bool(row.get("learningActive")) for row in rows)


def _transition_kind(previous_active: bool, current_active: bool) -> str:
    if current_active and not previous_active:
        return CHANGE_ACTIVATED
    if previous_active and not current_active:
        return CHANGE_DEACTIVATED
    return CHANGE_RECOMPUTED


def _has_non_neutral_value(row: Mapping[str, Any]) -> bool:
    return (
        bool(row.get("learningActive"))
        or float(row.get("netWeight") or 0.0) != 0.0
        or float(row.get("outcomeWeight") or 0.0) != 0.0
        or float(row.get("decisionWeight") or 0.0) != 0.0
        or int(row.get("signalCount") or 0) != 0
        or bool(_refs(row.get("contributingRefs")))
    )


def ensure_ranking_adjustment_tables() -> None:
    """Create the tables and indexes. Startup-only, like the sibling stores."""
    from database.models.ranking_adjustments import ALL_RANKING_ADJUSTMENT_DDL

    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                for statement in ALL_RANKING_ADJUSTMENT_DDL:
                    cur.execute(statement)
            con.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not ensure ranking adjustment tables: %s", exc)


# --------------------------------------------------------------------------
# Reading — the serving path
# --------------------------------------------------------------------------


def get_adjustments(
    org_id: str,
) -> Dict[Tuple[Optional[str], Optional[str]], GroupAdjustment]:
    """Every stored adjustment for one org, keyed for :func:`adjust_ranking`.

    Org-scoped in the WHERE clause, never filtered afterwards: AC6's isolation
    has to hold in the query or it does not hold.

    Never raises. A read failure yields an empty map, which serves BASE order —
    the safe direction, because an unavailable adjustment state must degrade to
    no learning rather than to stale or partial learning.
    """
    org = _clean(org_id)
    if not org:
        return {}
    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT detector_id, pack_id, net_weight, outcome_weight,"
                    "       decision_weight, has_outcome_evidence, signal_count,"
                    "       contributing_refs, learning_active"
                    "  FROM ranking_adjustments"
                    " WHERE org_id = %s AND learning_active = TRUE",
                    (org,),
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - serving must not depend on this
        logger.warning("Could not read ranking adjustments for %s: %s", org, exc)
        return {}

    out: Dict[Tuple[Optional[str], Optional[str]], GroupAdjustment] = {}
    for row in rows:
        refs = row[7]
        if isinstance(refs, (str, bytes)):
            try:
                refs = json.loads(refs)
            except (TypeError, ValueError):
                refs = []
        detector, pack = _from_key_part(row[0]), _from_key_part(row[1])
        out[(detector, pack)] = GroupAdjustment(
            detector_id=detector,
            pack_id=pack,
            net_weight=float(row[2] or 0.0),
            outcome_weight=float(row[3] or 0.0),
            decision_weight=float(row[4] or 0.0),
            has_outcome_evidence=bool(row[5]),
            signal_count=int(row[6] or 0),
            contributing_refs=tuple(
                dict(r) for r in (refs or ()) if isinstance(r, Mapping)
            ),
        )
    return out


def list_adjustment_state(org_id: str) -> List[Dict[str, Any]]:
    """The inspectable state, including inactive rows. T4's read model.

    Never raises, matching :func:`get_adjustments`: a transient DB error on a
    read-only governance surface degrades to an empty list rather than a 500,
    because the state panel is an inspection view and an unavailable one must not
    take the whole response down with it. The failure is logged rather than
    swallowed silently — an empty list here means "nothing readable", and the log
    line is what distinguishes that from "no state stored".
    """
    org = _clean(org_id)
    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT detector_id, pack_id, signal_concept, net_weight,"
                    "       outcome_weight, decision_weight, has_outcome_evidence,"
                    "       signal_count, learning_active, contributing_refs,"
                    "       config_version, revision, computed_at, updated_at"
                    "  FROM ranking_adjustments"
                    " WHERE org_id = %s"
                    " ORDER BY ABS(net_weight) DESC, detector_id ASC, pack_id ASC",
                    (org,),
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - an inspection view must still serve
        logger.warning("Could not read ranking adjustment state for %s: %s", org, exc)
        return []
    return [
        {
            "detectorId": _from_key_part(r[0]),
            "packId": _from_key_part(r[1]),
            "signalConcept": r[2],
            "netWeight": float(r[3] or 0.0),
            "outcomeWeight": float(r[4] or 0.0),
            "decisionWeight": float(r[5] or 0.0),
            "hasOutcomeEvidence": bool(r[6]),
            "signalCount": int(r[7] or 0),
            "learningActive": bool(r[8]),
            "contributingRefs": _refs(r[9]),
            "configVersion": r[10],
            "revision": int(r[11] or 1),
            "computedAt": r[12].isoformat() if r[12] else None,
            "updatedAt": r[13].isoformat() if r[13] else None,
        }
        for r in rows
    ]


def get_adjustment_history(
    org_id: str, *, limit: int = 200
) -> List[Dict[str, Any]]:
    """Every value this org's adjustments have held, newest first.

    Never raises, for the same reason as :func:`list_adjustment_state`: the audit
    history is a read-only governance view, so a transient DB error degrades to an
    empty list with a logged warning rather than a 500.
    """
    org = _clean(org_id)
    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT record FROM ranking_adjustment_history"
                    " WHERE org_id = %s ORDER BY recorded_at DESC, history_id DESC"
                    " LIMIT %s",
                    (org, max(1, min(int(limit), 1000))),
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - an audit view must still serve
        logger.warning("Could not read ranking adjustment history for %s: %s", org, exc)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        raw = row[0]
        if isinstance(raw, Mapping):
            out.append(dict(raw))
            continue
        try:
            parsed = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


# --------------------------------------------------------------------------
# Writing — explicit recomputation
# --------------------------------------------------------------------------


def recompute_adjustments(
    org_id: str,
    *,
    signal_set: Optional[Any] = None,
    actor_id: str = ACTOR_SYSTEM,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Recompute this org's adjustment state from T1's signal set.

    Deliberate, never on the serving path. Each group's previous value is
    carried into an append-only history row before the current row is replaced,
    so the sequence of values is reconstructable even though the current table is
    updated in place.

    Returns a summary: how many groups were written, whether learning was active,
    and the config version the values were computed under.
    """
    org = _clean(org_id)
    if not org:
        raise ValueError("org_id is required")

    config = load_config()
    when = now or datetime.now(timezone.utc)

    if signal_set is None:
        from .learning_signals import collect_learning_signals

        signal_set = collect_learning_signals(org)

    from .learning_signals import group_by_similarity

    groups = group_by_similarity(signal_set)
    active = bool(signal_set.is_active)

    previous_state = _safe_state(org)
    previous_active = _state_has_active(previous_state)
    org_change_kind = _transition_kind(previous_active, active)
    previous = {
        (row["detectorId"], row["packId"]): row for row in previous_state
    }

    written = 0
    seen_keys = set()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            for group in groups:
                detector = _key_part(group.key.detector_id)
                pack = _key_part(group.key.pack_id)
                state_key = (_from_key_part(detector), _from_key_part(pack))
                seen_keys.add(state_key)
                prior = previous.get(state_key)
                revision = int((prior or {}).get("revision") or 0) + 1
                refs = [dict(r) for r in group.contributing_refs]
                stored_net_weight = float(group.net_weight) if active else 0.0
                stored_outcome_weight = float(group.outcome_weight) if active else 0.0
                stored_decision_weight = float(group.decision_weight) if active else 0.0
                row_change_kind = _transition_kind(
                    bool((prior or {}).get("learningActive")), active
                )

                cur.execute(
                    "INSERT INTO ranking_adjustments ("
                    "  org_id, detector_id, pack_id, signal_concept, net_weight,"
                    "  outcome_weight, decision_weight, has_outcome_evidence,"
                    "  signal_count, learning_active, contributing_refs,"
                    "  config_version, revision, computed_at, updated_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (org_id, detector_id, pack_id) DO UPDATE SET"
                    "  signal_concept = EXCLUDED.signal_concept,"
                    "  net_weight = EXCLUDED.net_weight,"
                    "  outcome_weight = EXCLUDED.outcome_weight,"
                    "  decision_weight = EXCLUDED.decision_weight,"
                    "  has_outcome_evidence = EXCLUDED.has_outcome_evidence,"
                    "  signal_count = EXCLUDED.signal_count,"
                    "  learning_active = EXCLUDED.learning_active,"
                    "  contributing_refs = EXCLUDED.contributing_refs,"
                    "  config_version = EXCLUDED.config_version,"
                    "  revision = EXCLUDED.revision,"
                    "  updated_at = EXCLUDED.updated_at",
                    (
                        org,
                        detector,
                        pack,
                        group.key.signal_concept,
                        stored_net_weight,
                        stored_outcome_weight,
                        stored_decision_weight,
                        bool(group.has_outcome_evidence) if active else False,
                        len(group.signals),
                        active,
                        json.dumps(refs),
                        config.config_version,
                        revision,
                        when,
                        when,
                    ),
                )
                _append_history(
                    cur,
                    org_id=org,
                    detector_id=detector,
                    pack_id=pack,
                    change_kind=row_change_kind,
                    previous_net_weight=(prior or {}).get("netWeight"),
                    net_weight=stored_net_weight,
                    signal_count=len(group.signals),
                    learning_active=active,
                    actor_id=actor_id,
                    config_version=config.config_version,
                    revision=revision,
                    when=when,
                    extra={
                        "contributingRefs": refs,
                        "learningState": signal_set.activation_state(),
                    },
                )
                written += 1
            for key, row in previous.items():
                if key in seen_keys or not _has_non_neutral_value(row):
                    continue
                detector = _key_part(row.get("detectorId"))
                pack = _key_part(row.get("packId"))
                revision = int(row.get("revision") or 0) + 1
                row_change_kind = (
                    CHANGE_DEACTIVATED
                    if bool(row.get("learningActive"))
                    else CHANGE_RECOMPUTED
                )
                cur.execute(
                    "UPDATE ranking_adjustments SET"
                    "  net_weight = 0,"
                    "  outcome_weight = 0,"
                    "  decision_weight = 0,"
                    "  has_outcome_evidence = FALSE,"
                    "  signal_count = 0,"
                    "  learning_active = FALSE,"
                    "  contributing_refs = %s,"
                    "  config_version = %s,"
                    "  revision = %s,"
                    "  computed_at = %s,"
                    "  updated_at = %s"
                    " WHERE org_id = %s AND detector_id = %s AND pack_id = %s",
                    (
                        json.dumps([]),
                        config.config_version,
                        revision,
                        when,
                        when,
                        org,
                        detector,
                        pack,
                    ),
                )
                _append_history(
                    cur,
                    org_id=org,
                    detector_id=detector,
                    pack_id=pack,
                    change_kind=row_change_kind,
                    previous_net_weight=row.get("netWeight"),
                    net_weight=0.0,
                    signal_count=0,
                    learning_active=False,
                    actor_id=actor_id,
                    config_version=config.config_version,
                    revision=revision,
                    when=when,
                    extra={
                        "previousState": dict(row),
                        "learningState": signal_set.activation_state(),
                    },
                )
                written += 1
        con.commit()

    current = _safe_state(org)
    emit_ranking_adjustment_changed(
        org_id=org,
        actor_id=actor_id,
        change_kind=org_change_kind,
        previous_state=previous_state,
        current_state=current,
        groups_changed=written,
        opportunities_affected=_affected_opportunity_count(current),
        config_version=config.config_version,
        changed_at=when.isoformat(),
    )

    return {
        "schemaVersion": ADJUSTMENT_STATE_SCHEMA_VERSION,
        "orgId": org,
        "changeKind": org_change_kind,
        "groupsWritten": written,
        "learningActive": active,
        "inactiveReason": signal_set.inactive_reason,
        "learningState": signal_set.activation_state(),
        "configVersion": config.config_version,
        "computedAt": when.isoformat(),
    }


def reset_adjustments(
    org_id: str,
    *,
    actor_id: str,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Neutralise this org's current adjustment state and append reset history.

    Reset never rewrites history. The current table is the serving cache, so it
    is safe to update in place; the reset itself is preserved as a new history
    entry for every group that existed, or as an org-level marker when there was
    no current state to neutralise.
    """
    org = _clean(org_id)
    actor = _clean(actor_id)
    if not org:
        raise ValueError("org_id is required")
    if not actor:
        raise ValueError("actor_id is required")

    config = load_config()
    when = now or datetime.now(timezone.utc)
    previous = _safe_state(org)
    groups_reset = len(previous)
    opportunities_affected = _affected_opportunity_count(previous)
    reset_reason = _clean(reason)[:500] or None

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            if previous:
                for row in previous:
                    detector = _key_part(row.get("detectorId"))
                    pack = _key_part(row.get("packId"))
                    revision = int(row.get("revision") or 0) + 1
                    cur.execute(
                        "UPDATE ranking_adjustments SET"
                        "  net_weight = 0,"
                        "  outcome_weight = 0,"
                        "  decision_weight = 0,"
                        "  has_outcome_evidence = FALSE,"
                        "  signal_count = 0,"
                        "  learning_active = FALSE,"
                        "  contributing_refs = %s,"
                        "  config_version = %s,"
                        "  revision = %s,"
                        "  computed_at = %s,"
                        "  updated_at = %s"
                        " WHERE org_id = %s AND detector_id = %s AND pack_id = %s",
                        (
                            json.dumps([]),
                            config.config_version,
                            revision,
                            when,
                            when,
                            org,
                            detector,
                            pack,
                        ),
                    )
                    _append_history(
                        cur,
                        org_id=org,
                        detector_id=detector,
                        pack_id=pack,
                        change_kind=CHANGE_RESET,
                        previous_net_weight=row.get("netWeight"),
                        net_weight=0.0,
                        signal_count=0,
                        learning_active=False,
                        actor_id=actor,
                        config_version=config.config_version,
                        revision=revision,
                        when=when,
                        extra={
                            "resetReason": reset_reason,
                            "previousState": dict(row),
                        },
                    )
            else:
                _append_history(
                    cur,
                    org_id=org,
                    detector_id=_UNKNOWN,
                    pack_id=_UNKNOWN,
                    change_kind=CHANGE_RESET,
                    previous_net_weight=None,
                    net_weight=0.0,
                    signal_count=0,
                    learning_active=False,
                    actor_id=actor,
                    config_version=config.config_version,
                    revision=1,
                    when=when,
                    extra={
                        "resetReason": reset_reason,
                        "resetMarker": True,
                        "previousState": [],
                    },
                )
        con.commit()

    current = _safe_state(org)
    payload = {
        "schemaVersion": ADJUSTMENT_STATE_SCHEMA_VERSION,
        "orgId": org,
        "changeKind": CHANGE_RESET,
        "groupsReset": groups_reset,
        "opportunitiesAffected": opportunities_affected,
        "previousState": previous,
        "currentState": current,
        "configVersion": config.config_version,
        "resetAt": when.isoformat(),
        "actorId": actor,
    }
    if reset_reason:
        payload["reason"] = reset_reason

    emit_ranking_adjustment_changed(
        org_id=org,
        actor_id=actor,
        change_kind=CHANGE_RESET,
        previous_state=previous,
        current_state=current,
        groups_changed=groups_reset,
        opportunities_affected=opportunities_affected,
        config_version=config.config_version,
        changed_at=when.isoformat(),
        reason=reset_reason,
    )
    return payload


def _safe_state(org_id: str) -> List[Dict[str, Any]]:
    try:
        return list_adjustment_state(org_id)
    except Exception as exc:  # noqa: BLE001 - a first run has no table yet
        logger.debug("No prior adjustment state for %s: %s", org_id, exc)
        return []


def _append_history(
    cur: Any,
    *,
    org_id: str,
    detector_id: str,
    pack_id: str,
    change_kind: str,
    previous_net_weight: Optional[float],
    net_weight: float,
    signal_count: int,
    learning_active: bool,
    actor_id: str,
    config_version: Optional[str],
    revision: int,
    when: datetime,
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    history_id = f"radj_{uuid.uuid4().hex[:20]}"
    record = {
        "schemaVersion": ADJUSTMENT_STATE_SCHEMA_VERSION,
        "historyId": history_id,
        "orgId": org_id,
        "detectorId": _from_key_part(detector_id),
        "packId": _from_key_part(pack_id),
        "changeKind": change_kind,
        "previousNetWeight": previous_net_weight,
        "netWeight": net_weight,
        "signalCount": signal_count,
        "learningActive": learning_active,
        "actorId": actor_id,
        "configVersion": config_version,
        "revision": revision,
        "recordedAt": when.isoformat(),
    }
    if extra:
        record.update(dict(extra))
    cur.execute(
        "INSERT INTO ranking_adjustment_history ("
        "  history_id, org_id, detector_id, pack_id, change_kind,"
        "  previous_net_weight, net_weight, signal_count, learning_active,"
        "  actor_id, config_version, revision, record, recorded_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            history_id,
            org_id,
            detector_id,
            pack_id,
            change_kind,
            previous_net_weight,
            net_weight,
            signal_count,
            learning_active,
            actor_id,
            config_version,
            revision,
            json.dumps(record),
            when,
        ),
    )


__all__ = [
    "ACTOR_SYSTEM",
    "ADJUSTMENT_STATE_SCHEMA_VERSION",
    "CHANGE_ACTIVATED",
    "CHANGE_DEACTIVATED",
    "CHANGE_RECOMPUTED",
    "CHANGE_RESET",
    "ensure_ranking_adjustment_tables",
    "get_adjustment_history",
    "get_adjustments",
    "list_adjustment_state",
    "recompute_adjustments",
    "reset_adjustments",
]
