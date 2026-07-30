"""2.0-A2 T2 — write-once persistence for the frozen baseline artifact.

The store's whole contract is what it *cannot* do. There is no update function, no
upsert, no delete: the only write is an INSERT whose conflict clause is
``DO NOTHING``, so a second capture for an identity that already has a baseline is
a **no-op that reports itself**, never an overwrite.

That matters because the alternative is subtle and fatal. If a later run could
restate what a finding was born with, then every outcome claim built on it becomes
a claim about a moving target — and nothing in the output would look wrong.

Immutability is therefore enforced in three independent places, because a
convention is not an enforcement:

1. the primary key ``(org_id, opportunity_identity)`` makes a rewrite a conflict;
2. this module contains no UPDATE/DELETE statement, proven by a contract test that
   greps it rather than trusting review;
3. production grants remove UPDATE/DELETE on the table entirely (see the DDL
   module), the same posture 2.0-D4 T4 will verify schema-wide.

Every key and query includes ``org_id``; a cross-org read returns nothing rather
than revealing that the identity exists in another tenant.

Backfill for pre-existing opportunities is explicitly out of scope. A finding
created before this subtask has no baseline and is therefore never measurable —
which is the honest outcome, not a gap to paper over with a reconstructed basis.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import db
from .opportunity_baseline_artifact import (
    BaselineCaptureError,
    build_baseline_artifact,
)
from database.models.opportunity_baselines import ALL_OPPORTUNITY_BASELINES_DDL

logger = logging.getLogger(__name__)

_TABLE_READY = False


def ensure_opportunity_baseline_table() -> None:
    """Create the baseline table if absent (idempotent, never raises).

    Migration ``0032`` is the authoritative creator; this is the dev-DB safety net,
    mirroring ``ensure_opportunity_lifecycle_tables()``. The existence check is
    READ-ONLY first because production runs under a role with no CREATE on the
    schema, where issuing ``CREATE TABLE`` is rejected even with IF NOT EXISTS.
    """
    global _TABLE_READY
    if _TABLE_READY:
        return
    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                    ("opportunity_baselines",),
                )
                if cur.fetchone() is None:
                    for ddl in ALL_OPPORTUNITY_BASELINES_DDL:
                        cur.execute(ddl)
            con.commit()
        _TABLE_READY = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ensure_opportunity_baseline_table skipped (assuming provisioned): %s",
            exc,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_SELECT = (
    "SELECT org_id, opportunity_identity, run_id, detector_id, pack_id, "
    "pack_version, opportunity_ref, window_days, window_started_at, "
    "window_ended_at, window_derivation, schema_version, artifact, captured_at "
    "FROM opportunity_baselines"
)


def _row_to_artifact(row: Sequence[Any]) -> Dict[str, Any]:
    """Return the stored artifact JSON, with the promoted columns as the truth.

    The JSON body is what was frozen; the columns are a queryable projection of
    it. On the (impossible-by-design) chance they disagree, the JSON wins because
    it is the record that was written once.
    """
    try:
        artifact = json.loads(row[12]) if row[12] else {}
    except (TypeError, ValueError):
        logger.warning(
            "Stored baseline artifact for %s is not valid JSON", row[1]
        )
        artifact = {}
    if not isinstance(artifact, dict):
        artifact = {}
    return artifact


# --------------------------------------------------------------------------
# Capture — the only write
# --------------------------------------------------------------------------


def capture_baseline(
    opp: Mapping[str, Any],
    *,
    org_id: str,
    run_id: str,
    captured_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Freeze one finding's measurement basis, if it does not already have one.

    Returns ``{"created": bool, "artifact": dict}``. ``created=False`` means a
    baseline already existed and was left exactly as it was — the no-op the
    definition of done requires, reported rather than silent so a caller can tell
    "already frozen" from "just frozen".

    Raises :class:`BaselineCaptureError` when the opportunity cannot produce an
    honest artifact (no stable identity, no detector). The caller decides whether
    that is fatal; the pipeline treats it as non-blocking.
    """
    artifact = build_baseline_artifact(
        opp, org_id=org_id, run_id=run_id, captured_at=captured_at or _now_iso()
    )
    identity = artifact["opportunityIdentity"]
    window = artifact.get("window") or {}

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_baselines ("
                "  org_id, opportunity_identity, run_id, detector_id, pack_id,"
                "  pack_version, opportunity_ref, window_days, window_started_at,"
                "  window_ended_at, window_derivation, schema_version, artifact,"
                "  captured_at"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                # DO NOTHING, never DO UPDATE. This clause IS the immutability
                # guarantee at the statement level: a second capture for the same
                # identity changes nothing and reports rowcount 0.
                "ON CONFLICT (org_id, opportunity_identity) DO NOTHING",
                (
                    artifact["orgId"],
                    identity,
                    artifact["runId"],
                    artifact["detectorId"],
                    artifact.get("packId"),
                    artifact.get("packVersion"),
                    artifact.get("opportunityRef"),
                    window.get("days"),
                    _parse_ts(window.get("startedAt")),
                    _parse_ts(window.get("endedAt")),
                    window.get("derivation") or "unknown",
                    artifact["schemaVersion"],
                    json.dumps(artifact),
                    _parse_ts(artifact["capturedAt"]),
                ),
            )
            created = cur.rowcount == 1
        con.commit()

    if created:
        return {"created": True, "artifact": artifact}

    # Already frozen — return the EXISTING artifact, not the one just built, so a
    # caller never mistakes a freshly-computed basis for the stored one.
    existing = get_baseline(org_id, identity)
    return {"created": False, "artifact": existing}


def capture_baselines_for_run(
    opps: Sequence[Mapping[str, Any]],
    *,
    org_id: str,
    run_id: str,
) -> Dict[str, int]:
    """Freeze a baseline for every opportunity a run surfaced. Non-blocking.

    Returns ``{"created", "existing", "skipped"}``. A finding that cannot produce
    an honest artifact is SKIPPED and counted, never given a fabricated one, and
    never allowed to fail the run.
    """
    captured_at = _now_iso()
    counts = {"created": 0, "existing": 0, "skipped": 0}
    for opp in opps or ():
        if not isinstance(opp, Mapping):
            counts["skipped"] += 1
            continue
        try:
            result = capture_baseline(
                opp, org_id=org_id, run_id=run_id, captured_at=captured_at
            )
        except BaselineCaptureError as exc:
            counts["skipped"] += 1
            logger.info(
                "No baseline captured for opportunity %s: %s", opp.get("id"), exc
            )
            continue
        except Exception as exc:  # noqa: BLE001 - never break a run
            counts["skipped"] += 1
            logger.warning(
                "Baseline capture failed for opportunity %s (non-blocking): %s",
                opp.get("id"),
                exc,
            )
            continue
        counts["created" if result["created"] else "existing"] += 1
    return counts


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_baseline(org_id: str, opportunity_identity: str) -> Optional[Dict[str, Any]]:
    """The frozen artifact for one identity, or ``None`` when this org has none."""
    org = str(org_id or "").strip()
    identity = str(opportunity_identity or "").strip()
    if not org or not identity:
        return None

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s AND opportunity_identity = %s",
                (org, identity),
            )
            row = cur.fetchone()
    return _row_to_artifact(row) if row else None


def has_baseline(org_id: str, opportunity_identity: str) -> bool:
    """Whether a finding has a frozen basis — T7's other gate.

    An opportunity with no baseline can never be measured, however much its
    signals move, because there is nothing defensible to compare against.
    """
    return get_baseline(org_id, opportunity_identity) is not None


def get_baselines_for_run(org_id: str, run_id: str) -> Dict[str, Dict[str, Any]]:
    """Every baseline CREATED by one run, keyed by opportunity identity."""
    org, run = str(org_id or "").strip(), str(run_id or "").strip()
    if not org or not run:
        return {}

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s AND run_id = %s ORDER BY captured_at ASC",
                (org, run),
            )
            rows = cur.fetchall()
    return {row[1]: _row_to_artifact(row) for row in rows}


def list_baselines(org_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
    """Every frozen baseline in one org, newest first."""
    org = str(org_id or "").strip()
    if not org:
        return []

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s ORDER BY captured_at DESC LIMIT %s",
                (org, max(1, min(int(limit), 1000))),
            )
            rows = cur.fetchall()
    return [_row_to_artifact(row) for row in rows]


__all__ = [
    "BaselineCaptureError",
    "capture_baseline",
    "capture_baselines_for_run",
    "ensure_opportunity_baseline_table",
    "get_baseline",
    "get_baselines_for_run",
    "has_baseline",
    "list_baselines",
]
