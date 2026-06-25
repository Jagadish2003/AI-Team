"""Opportunity-instance storage + retrieval (R16-B1, Part Two / T4).

Persists one per-run observation (an *instance*) of each opportunity, keyed by
the stable ``opportunity_identity`` (R16-B1 §2) plus the ``run_id``. The same
real-world problem keeps the same identity across runs, while every run gets its
own instance recording how the problem looked at that time (score, confidence,
evidence, narrative). Querying by identity returns the full cross-run time
series — the foundation outcome tracking (2.0) builds on.

Design:
  * ``build_opportunity_instance`` is a pure function (no DB) — given a runner /
    Track A opportunity dict + run id it produces an :class:`OpportunityInstance`,
    computing the identity from run-invariant inputs when the upstream opp has
    not already stamped one (T3 compatibility), and stamping a pack version,
    falling back to ``DEFAULT_PACK_VERSION`` when T5's stamp is absent (AC6).
  * ``record_opportunity_instances`` writes them, NON-BLOCKING: a storage failure
    is logged and never breaks a discovery run (same contract as entity
    extraction).
  * ``get_instances_by_identity`` / ``get_instances_for_run`` read them back.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from . import db
from discovery.opportunity_identity import (
    compute_opportunity_identity,
    primary_entity_keys_for_detector,
)
from database.models.opportunity_instances import (
    ALL_OPPORTUNITY_INSTANCES_DDL,
    DEFAULT_PACK_VERSION,
    OPPORTUNITY_INSTANCE_COLUMNS,
    OpportunityInstance,
)

logger = logging.getLogger(__name__)

_TABLE_READY = False


def ensure_opportunity_instances_table() -> None:
    """Create the opportunity_instances table + indexes if absent (idempotent).

    The authoritative creator is migration ``0017`` (run by alembic in tests and
    by provisioning in prod); this runtime helper is a safety net so the write
    path works on a dev DB that has not yet been migrated — mirroring
    ``ensure_entities_table()``. Runs at most once per process. Never raises: on
    a least-privilege role without CREATE the table already exists from
    provisioning, so the subsequent write still succeeds.
    """
    global _TABLE_READY
    if _TABLE_READY:
        return
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            for ddl in ALL_OPPORTUNITY_INSTANCES_DDL:
                cur.execute(ddl)
            con.commit()
        finally:
            con.close()
        _TABLE_READY = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ensure_opportunity_instances_table skipped (assuming provisioned): %s",
            exc,
        )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _detector_id_of(opp: Dict[str, Any]) -> str:
    return (
        opp.get("detector_id")
        or (opp.get("_debug") or {}).get("detector_id")
        or ""
    )


def _signal_source_of(opp: Dict[str, Any]) -> str:
    return (
        opp.get("signal_source")
        or (opp.get("_debug") or {}).get("signal_source")
        or ""
    )


def build_opportunity_instance(
    opp: Dict[str, Any],
    run_id: str,
    *,
    org_id: Optional[str] = None,
) -> OpportunityInstance:
    """Build a per-run :class:`OpportunityInstance` from an opportunity dict.

    Reads both the raw-runner opp shape (``orgId``/``detector_id`` at top level)
    and the Track A stored shape (``detector_id`` under ``_debug``) defensively.

    The ``opportunity_identity`` is taken from the opp when already stamped (T3),
    otherwise computed from its run-invariant inputs so this works whether or not
    T3's runner wiring is present. ``pack_version`` uses the opp's stamp (T5) or
    falls back to ``DEFAULT_PACK_VERSION`` — the AC6 column is never empty.

    Raises ``ValueError`` only when a required identity input (org/pack/detector)
    is missing — the caller records per-opp and skips a malformed one.
    """
    resolved_org = org_id or opp.get("orgId") or opp.get("org_id") or "default"
    pack_id = opp.get("packId") or opp.get("pack_id") or ""
    detector_id = _detector_id_of(opp)
    signal_source = _signal_source_of(opp)

    identity = opp.get("opportunity_identity") or compute_opportunity_identity(
        org_id=resolved_org,
        pack_id=pack_id,
        signal_key=detector_id,
        primary_entity_ids=primary_entity_keys_for_detector(detector_id, signal_source),
    )

    pack_version = (
        opp.get("packVersion") or opp.get("pack_version") or DEFAULT_PACK_VERSION
    )

    evidence_ids = list(opp.get("evidenceIds") or [])
    score_debug = opp.get("score_debug") or {}
    score = _float_or_none(opp.get("score"))
    if score is None:
        # Best-effort: some packs carry a composite under score_debug.
        for k in ("final_score", "score", "total", "composite"):
            if k in score_debug:
                score = _float_or_none(score_debug[k])
                break

    narrative = (
        opp.get("aiRationale")
        or opp.get("description")
        or opp.get("title")
        or None
    )

    return OpportunityInstance(
        opportunity_identity=identity,
        run_id=run_id,
        org_id=resolved_org,
        pack_id=pack_id,
        pack_version=pack_version,
        detector_id=detector_id,
        signal_source=signal_source or None,
        opportunity_ref=opp.get("id"),
        impact=_int_or_none(opp.get("impact")),
        effort=_int_or_none(opp.get("effort")),
        score=score,
        confidence=opp.get("confidence"),
        tier=opp.get("tier"),
        evidence_ids=evidence_ids,
        evidence_count=len(evidence_ids),
        narrative=narrative,
    )


def stamp_opportunity_identities(
    opps: Iterable[Dict[str, Any]],
    run_id: str,
    *,
    org_id: Optional[str] = None,
) -> int:
    """Stamp the stable ``opportunity_identity`` onto each opportunity in place.

    This is the "store the stable identity on every opportunity" half of R16-B1
    §2 — the served opportunity record carries the cross-run id, so the UI and
    later features can group an opportunity with its own history. Idempotent and
    additive: an opp that already carries an identity (e.g. T3's runner wiring is
    present) keeps the same deterministic value. Never raises — a malformed opp
    is skipped. Returns the number stamped.
    """
    stamped = 0
    for opp in opps or []:
        try:
            opp["opportunity_identity"] = build_opportunity_instance(
                opp, run_id, org_id=org_id
            ).opportunity_identity
            stamped += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not stamp opportunity_identity (opp %s): %s",
                opp.get("id"), exc,
            )
    return stamped


def record_opportunity_instances(
    run_id: str,
    opps: Iterable[Dict[str, Any]],
    *,
    org_id: Optional[str] = None,
) -> int:
    """Persist one opportunity_instance per opportunity for this run.

    Upserts on the ``(opportunity_identity, run_id)`` primary key, so a re-run /
    replay of the same run refreshes its instances rather than duplicating them.
    NON-BLOCKING: any failure (a malformed opp, a missing table, a DB error) is
    logged and skipped — recording instances must never break a discovery run.
    Returns the number of instances successfully written.
    """
    opp_list = list(opps or [])
    if not opp_list:
        return 0

    ensure_opportunity_instances_table()

    insert_cols = ", ".join(OPPORTUNITY_INSTANCE_COLUMNS)
    placeholders = ", ".join(["%s"] * len(OPPORTUNITY_INSTANCE_COLUMNS))
    update_cols = ", ".join(
        f"{c}=EXCLUDED.{c}"
        for c in OPPORTUNITY_INSTANCE_COLUMNS
        if c not in ("opportunity_identity", "run_id")
    )
    sql = (
        f"INSERT INTO opportunity_instances ({insert_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (opportunity_identity, run_id) DO UPDATE SET {update_cols}"
    )

    written = 0
    con = None
    try:
        con = db.connect()
        cur = con.cursor()
        for opp in opp_list:
            try:
                instance = build_opportunity_instance(opp, run_id, org_id=org_id)
                row = instance.to_db_row()
                cur.execute(sql, [row[c] for c in OPPORTUNITY_INSTANCE_COLUMNS])
                written += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Skipping opportunity_instance for run %s (opp %s): %s",
                    run_id, opp.get("id"), exc,
                )
        con.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_opportunity_instances failed for run %s (non-blocking): %s",
            run_id, exc,
        )
        if con is not None:
            try:
                con.rollback()
            except Exception:
                pass
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    return written


def _rows_to_instances(rows: Iterable[Any]) -> List[OpportunityInstance]:
    """Map SELECTed rows to instances by the fixed column order.

    Rows come back as psycopg2 ``DictRow`` (a list subclass) under the app's
    DictCursor; positional mapping from ``OPPORTUNITY_INSTANCE_COLUMNS`` (the same
    order the SELECT uses) yields a plain dict that works regardless of cursor
    factory — ``dict(DictRow)`` would NOT key by column name.
    """
    instances: List[OpportunityInstance] = []
    for r in rows:
        row = {col: r[i] for i, col in enumerate(OPPORTUNITY_INSTANCE_COLUMNS)}
        instances.append(OpportunityInstance.from_db_row(row))
    return instances


def get_instances_by_identity(
    opportunity_identity: str,
    *,
    org_id: Optional[str] = None,
) -> List[OpportunityInstance]:
    """Return every instance sharing one ``opportunity_identity``, oldest first.

    This is the cross-run time series for a single underlying problem — the read
    outcome tracking (2.0) compares to ask whether a problem improved, worsened,
    disappeared, or stayed the same. Optionally scoped to one org.
    """
    if not opportunity_identity:
        return []
    cols = ", ".join(OPPORTUNITY_INSTANCE_COLUMNS)
    sql = f"SELECT {cols} FROM opportunity_instances WHERE opportunity_identity = %s"
    params: List[Any] = [opportunity_identity]
    if org_id is not None:
        sql += " AND org_id = %s"
        params.append(org_id)
    sql += " ORDER BY created_at ASC, run_id ASC"

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        con.close()
    return _rows_to_instances(rows)


def get_instances_for_run(
    run_id: str,
    *,
    org_id: Optional[str] = None,
) -> List[OpportunityInstance]:
    """Return all opportunity instances observed in a single run."""
    if not run_id:
        return []
    cols = ", ".join(OPPORTUNITY_INSTANCE_COLUMNS)
    sql = f"SELECT {cols} FROM opportunity_instances WHERE run_id = %s"
    params: List[Any] = [run_id]
    if org_id is not None:
        sql += " AND org_id = %s"
        params.append(org_id)
    sql += " ORDER BY opportunity_identity ASC"

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        con.close()
    return _rows_to_instances(rows)
