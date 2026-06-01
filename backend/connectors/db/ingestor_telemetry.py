"""
T2-S11-A  |  DB ingestor telemetry emitter
AgentIQ 2.0  |  Track 2 — Enterprise Technology  |  Sprint 11

Provides ``emit_ingestor_completed()``: the single call-site that every
Track 2 database ingestor (SQL Server, Oracle DB, PostgreSQL) uses to
emit the ``db.ingestor_completed`` telemetry event when ingestion finishes.

Design rules
------------
* **Fire-and-forget.**  emit_ingestor_completed() never raises.  Any
  internal failure (DB unavailable, serialisation error, import error) is
  caught, logged at ERROR level, and silently swallowed.  The ingestor's
  return value is never affected by telemetry failures.

* **Locked event type.**  The event type string ``'db.ingestor_completed'``
  is registered in the T1-S10-C telemetry registry (app/telemetry.py).
  It is NOT re-registered here — importing this module is safe to do from
  any ingestor without risk of double-registration errors.

* **Payload shape.**  The payload follows ``DBIngestorCompletedPayload``
  (doc: T2-S11-A Section 4):
      connector_id   — 'sqlserver' | 'oracle_db' | 'postgresql'
      pack_id        — e.g. 'sqlserver_opsignal'
      query_count    — number of execute_query() calls attempted
      signal_count   — number of signal metrics successfully extracted
      degraded_count — number of metrics with degraded_signal=True
      duration_ms    — wall-clock time for the full ingestor run

Usage (from SQL Server ingestor)
---------------------------------
::

    from connectors.db.ingestor_telemetry import emit_ingestor_completed

    start_ms = time.monotonic()
    result   = _run_queries(config, scope, org_id, run_id)
    duration = int((time.monotonic() - start_ms) * 1000)

    emit_ingestor_completed(
        org_id        = org_id,
        run_id        = run_id,
        connector_id  = "sqlserver",
        pack_id       = "sqlserver_opsignal",
        query_count   = 3,
        signal_count  = _count_signals(result),
        degraded_count = _count_degraded(result),
        duration_ms   = duration,
    )
"""

from __future__ import annotations

import logging
import traceback

from app.telemetry import record_event  # module-level import enables patching in tests

logger = logging.getLogger(__name__)

# Event type — registered in T1-S10-C registry (app/telemetry.py).
_EVENT_TYPE: str = "db.ingestor_completed"


def emit_ingestor_completed(
    *,
    org_id: str,
    run_id: str,
    connector_id: str,
    pack_id: str,
    query_count: int,
    signal_count: int,
    degraded_count: int,
    duration_ms: int,
) -> None:
    """Emit a ``db.ingestor_completed`` telemetry event.

    Parameters
    ----------
    org_id:
        Tenant/workspace identifier.
    run_id:
        Discovery run that triggered the ingestor.
    connector_id:
        Database connector ID, e.g. ``'sqlserver'``.
    pack_id:
        Detector pack that consumed the ingested signals,
        e.g. ``'sqlserver_opsignal'``.
    query_count:
        Number of ``execute_query()`` calls attempted during this run
        (counts both successful and failed calls).
    signal_count:
        Number of signal metrics successfully extracted (non-degraded).
    degraded_count:
        Number of metrics where ``degraded_signal=True`` was set due to
        a query timeout, missing column, or other partial-failure condition.
    duration_ms:
        Total wall-clock time for the ingestor run in milliseconds.

    Returns
    -------
    None
        Always.  This function never raises under any circumstance.

    Notes
    -----
    Telemetry failures are logged at ERROR level but never propagated.
    The ingestor return value is always unaffected by telemetry failures.
    """
    try:
        payload = {
            "org_id":          org_id,
            "run_id":          run_id,
            "source":          "connector",
            "connector_id":    connector_id,
            "pack_id":         pack_id,
            "query_count":     query_count,
            "signal_count":    signal_count,
            "degraded_count":  degraded_count,
            "duration_ms":     duration_ms,
            "success":         degraded_count < query_count,
            "count":           signal_count,
        }

        record_event(_EVENT_TYPE, payload)

    except Exception:
        logger.error(
            "emit_ingestor_completed failed silently — connector_id=%s run_id=%s\n%s",
            connector_id,
            run_id,
            traceback.format_exc(),
        )


def count_degraded_signals(ingestor_result: dict) -> int:
    """Count how many signal sections in *ingestor_result* have degraded_signal=True.

    Convenience helper for ingestors to compute ``degraded_count`` before
    calling ``emit_ingestor_completed()``.

    Parameters
    ----------
    ingestor_result:
        The full dict returned by the ingestor (Section 1d return shape).
        Looks for ``degraded_signal`` key inside each value that is a dict.

    Returns
    -------
    int
        Number of top-level sections (ticket_volume, sla_breach, queue_depth,
        etc.) that have ``degraded_signal=True``.
    """
    count = 0
    for value in ingestor_result.values():
        if isinstance(value, dict) and value.get("degraded_signal") is True:
            count += 1
    return count


def count_signal_metrics(ingestor_result: dict) -> int:
    """Count non-degraded signal sections in *ingestor_result*.

    Parameters
    ----------
    ingestor_result:
        The full ingestor return dict.

    Returns
    -------
    int
        Number of top-level dict sections where ``degraded_signal`` is
        False or absent (i.e. clean signals).
    """
    count = 0
    for value in ingestor_result.values():
        if isinstance(value, dict) and not value.get("degraded_signal", False):
            count += 1
    return count
