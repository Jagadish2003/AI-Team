"""2.0-A1 T6 — storing and retrieving the final projection.

**This module is load-bearing for 2.0-A2.** Without a stored projection there is
nothing to compare a measured outcome against, and the closed loop the whole of
Release 2.0 is built around never starts. Everything here exists so a projection
made today can be found, identified, and compared months later.

Two storage locations, deliberately:

1. **On the opportunity, in run-scoped KV** (``opps``). This is what every read
   surface already serves — the opportunities API, the roadmap, the executive
   report, the blueprint, the PDF export. It is the copy an analyst sees.

2. **On the opportunity INSTANCE row** (``opportunity_instances.metadata``,
   R16-B1 Part Two). Keyed by ``(opportunity_identity, run_id)``, this is the
   copy 2.0-A2 reads: it is queryable *across runs by identity*, which run-scoped
   KV is not. Following one problem through time is exactly what outcome
   tracking does, and run KV cannot answer "show me every projection ever made
   about this problem".

The duplication is intentional and worth stating: KV is the *serving* copy,
the instance row is the *tracking* copy. They are written from the same payload
in the same pipeline step, so they cannot disagree about what was projected.

The instance write is NON-BLOCKING, matching the contract every other Stage-2
writer here follows: a storage failure is logged and never breaks a discovery
run. The KV copy is the one the run depends on, and it is written first.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import db
from discovery.projection.provenance import (
    build_provenance,
    get_provenance,
    projection_core,
    stamp_projection,
)

logger = logging.getLogger(__name__)

#: Key the projection occupies inside ``opportunity_instances.metadata``.
METADATA_PROJECTION_KEY = "projection"


def _now_iso() -> str:
    """The one clock read in the projection path, isolated here on purpose."""
    return datetime.now(timezone.utc).isoformat()


def _opp_field(opp: Dict[str, Any], *names: str) -> Optional[Any]:
    """First present value among ``names``, checking ``_debug`` as a fallback."""
    debug = opp.get("_debug") or {}
    for name in names:
        if opp.get(name) not in (None, ""):
            return opp.get(name)
        if isinstance(debug, dict) and debug.get(name) not in (None, ""):
            return debug.get(name)
    return None


def stamp_projections(
    opps: List[Dict[str, Any]],
    run_id: str,
    *,
    org_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> int:
    """Stamp provenance onto every already-computed projection, in place.

    Called after ``project_opportunities``: the projection CONTENT is computed
    deterministically with no clock and no run context, then identified here.
    Keeping the two steps apart is what lets a recomputation reproduce a stored
    projection's core exactly (AC5) while the stored copy still carries when and
    where it came from (AC6).

    Returns the number of projections stamped.
    """
    from discovery.projection import (
        BAND_WIDTH_MODEL_VERSION,
        PROJECTION_SCHEMA_VERSION,
        RECOMMENDATION_SCHEMA_VERSION,
    )

    timestamp = created_at or _now_iso()
    stamped = 0

    for opp in opps or []:
        if not isinstance(opp, dict):
            continue
        projection = opp.get("projection")
        if not isinstance(projection, dict):
            continue
        try:
            provenance = build_provenance(
                run_id=run_id,
                opp_id=str(opp.get("id") or ""),
                created_at=timestamp,
                org_id=org_id or _opp_field(opp, "orgId", "org_id"),
                pack_id=_opp_field(opp, "packId", "pack_id"),
                pack_version=_opp_field(opp, "packVersion", "pack_version"),
                opportunity_identity=opp.get("opportunity_identity"),
                projection_schema_version=PROJECTION_SCHEMA_VERSION,
                band_width_model_version=BAND_WIDTH_MODEL_VERSION,
                recommendation_schema_version=RECOMMENDATION_SCHEMA_VERSION,
            )
        except Exception as exc:  # noqa: BLE001 - never break a run over a stamp
            logger.warning(
                "Could not build projection provenance (opp %s): %s",
                opp.get("id"),
                exc,
            )
            continue
        opp["projection"] = stamp_projection(projection, provenance)
        stamped += 1

    return stamped


def record_projections_on_instances(
    opps: List[Dict[str, Any]], run_id: str
) -> int:
    """Persist each stored projection onto its opportunity-instance row.

    The cross-run tracking copy. Merges into the existing ``metadata`` JSON
    rather than overwriting it, so an instance's other metadata survives.

    Non-blocking by contract — a failure here is logged and never fails a run.
    Returns the number of instance rows updated.
    """
    projected = [
        opp
        for opp in opps or []
        if isinstance(opp, dict) and isinstance(opp.get("projection"), dict)
    ]
    if not projected:
        return 0

    updated = 0
    try:
        from contextlib import closing

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                for opp in projected:
                    identity = opp.get("opportunity_identity")
                    if not identity:
                        # No stable identity => no cross-run row to attach to.
                        # The KV copy still holds the projection, so nothing is
                        # lost for this run; it just cannot be followed forward.
                        continue
                    cur.execute(
                        "SELECT metadata FROM opportunity_instances "
                        "WHERE opportunity_identity = %s AND run_id = %s",
                        (identity, run_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        continue
                    try:
                        metadata = json.loads(row[0]) if row[0] else {}
                        if not isinstance(metadata, dict):
                            metadata = {}
                    except (TypeError, ValueError):
                        metadata = {}
                    metadata[METADATA_PROJECTION_KEY] = opp["projection"]
                    cur.execute(
                        "UPDATE opportunity_instances SET metadata = %s "
                        "WHERE opportunity_identity = %s AND run_id = %s",
                        (json.dumps(metadata), identity, run_id),
                    )
                    updated += 1
            con.commit()
    except Exception as exc:  # noqa: BLE001 - storage failure never breaks a run
        logger.warning(
            "Could not record projections on opportunity instances (run %s): %s",
            run_id,
            exc,
        )
        return 0

    logger.info(
        "Recorded %d projection(s) onto opportunity instances for run %s",
        updated,
        run_id,
    )
    return updated


# ---------------------------------------------------------------------------
# Retrieval — the 2.0-A2 read surface.
# ---------------------------------------------------------------------------


def get_stored_projection(run_id: str, opp_id: str) -> Optional[Dict[str, Any]]:
    """The projection stored with one opportunity in one run.

    Reads the run-scoped KV copy — the same bytes every API surface serves — so
    what 2.0-A2 validates against is exactly what the analyst was shown.
    """
    opps = db.run_kv_get("opps", run_id, []) or []
    for opp in opps:
        if isinstance(opp, dict) and str(opp.get("id")) == str(opp_id):
            projection = opp.get("projection")
            return dict(projection) if isinstance(projection, dict) else None
    return None


def get_instance_projection(
    run_id: str,
    opportunity_identity: str,
    org_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The projection stored on the opportunity-instance tracking row.

    This is a read fallback for serve paths. The run KV copy remains the primary
    analyst-facing copy, but older/interrupted materializations can have the
    tracking copy while the served opportunity no longer carries ``projection``.
    Reading it preserves the "stored projection only, never recompute" rule.
    """
    if not run_id or not opportunity_identity:
        return None

    sql = (
        "SELECT metadata FROM opportunity_instances "
        "WHERE opportunity_identity = %s AND run_id = %s AND is_deleted = FALSE"
    )
    params: List[Any] = [opportunity_identity, run_id]
    if org_id:
        sql += " AND org_id = %s"
        params.append(org_id)

    try:
        from contextlib import closing

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(sql, tuple(params))
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - projection fallback is advisory
        logger.warning(
            "Could not read instance projection for identity %s in run %s: %s",
            opportunity_identity,
            run_id,
            exc,
        )
        return None

    if row is None:
        return None
    try:
        metadata = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        return None
    projection = (metadata or {}).get(METADATA_PROJECTION_KEY)
    return dict(projection) if isinstance(projection, dict) else None


def projection_for_opportunity(
    opp: Optional[Dict[str, Any]],
    run_id: str,
    org_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the stored projection for an opportunity, with instance fallback."""
    if not isinstance(opp, dict):
        return None
    projection = opp.get("projection")
    if isinstance(projection, dict):
        return dict(projection)

    identity = opp.get("opportunity_identity") or opp.get("opportunityIdentity")
    if not identity:
        return None
    return get_instance_projection(run_id, str(identity), org_id=org_id)


def get_projections_for_run(run_id: str) -> Dict[str, Dict[str, Any]]:
    """Every stored projection in one run, keyed by opportunity id."""
    opps = db.run_kv_get("opps", run_id, []) or []
    return {
        str(opp.get("id")): dict(opp["projection"])
        for opp in opps
        if isinstance(opp, dict) and isinstance(opp.get("projection"), dict)
    }


def get_projection_history(
    opportunity_identity: str, org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Every projection ever stored for ONE opportunity identity, oldest first.

    The cross-run read 2.0-A2 is built on: "what did we project about this
    problem, and when". Returns a list of ``{runId, createdAt, projection}``
    ordered by the instance's ``created_at`` so a caller can walk the series
    forward without re-sorting.

    Returns ``[]`` — never raises — when the table is absent or unreadable, so a
    caller on a dev DB without migrations degrades rather than erroring.
    """
    from contextlib import closing

    sql = (
        "SELECT run_id, created_at, metadata FROM opportunity_instances "
        "WHERE opportunity_identity = %s AND is_deleted = FALSE"
    )
    params: List[Any] = [opportunity_identity]
    if org_id:
        sql += " AND org_id = %s"
        params.append(org_id)
    sql += " ORDER BY created_at ASC"

    history: List[Dict[str, Any]] = []
    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not read projection history for identity %s: %s",
            opportunity_identity,
            exc,
        )
        return []

    for run_id, created_at, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json) if metadata_json else {}
        except (TypeError, ValueError):
            continue
        projection = (metadata or {}).get(METADATA_PROJECTION_KEY)
        if not isinstance(projection, dict):
            continue
        history.append(
            {
                "runId": run_id,
                "createdAt": (
                    created_at.isoformat()
                    if hasattr(created_at, "isoformat")
                    else created_at
                ),
                "projection": projection,
            }
        )
    return history


def projection_matches_stored(
    stored: Optional[Dict[str, Any]], recomputed: Optional[Dict[str, Any]]
) -> bool:
    """True when a recomputed projection matches a stored one in SUBSTANCE.

    Compares cores, not whole payloads: provenance differs by design on every
    recomputation (different timestamp), so a whole-payload comparison would
    report a false difference every time. Exported so 2.0-A2 does not have to
    rediscover this rule — getting it wrong would make every stored projection
    look stale.
    """
    return projection_core(stored) == projection_core(recomputed)


__all__ = [
    "METADATA_PROJECTION_KEY",
    "get_instance_projection",
    "get_projection_history",
    "get_projections_for_run",
    "get_stored_projection",
    "projection_for_opportunity",
    "projection_matches_stored",
    "record_projections_on_instances",
    "stamp_projections",
]
