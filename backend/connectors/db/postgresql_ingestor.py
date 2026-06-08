"""
PostgreSQL operational signal ingestor — T2-S12-A Task T2.

Runs three signal queries against the declared PostgreSQL scope and returns
structured signals consumed by the sqlserver_opsignal detector set.

Called by the runner when 'postgresql' is in the selected systems list.
All data access goes through execute_query() — no direct connection opened.

Driver:   psycopg2-binary. No native client required.
Quoting:  "" double-quotes for all identifiers (PostgreSQL standard).
Dates:    NOW() - INTERVAL '90 days', NOW() - INTERVAL '30 days'.
Boolean:  CASE WHEN "col" = TRUE (native). Falls back to CASE WHEN "col" = 1
          on type error and logs a warning.
"""

from __future__ import annotations

import logging
import time
from typing import Any

try:
    from backend.connectors.db import (
        DBConnectionError,
        DBConnectorConfig,
        ScopeDeclaration,
        execute_query,
        get_scope,
    )
    from backend.app.telemetry import record_event
except ModuleNotFoundError:
    from connectors.db import (
        DBConnectionError,
        DBConnectorConfig,
        ScopeDeclaration,
        execute_query,
        get_scope,
    )
    from app.telemetry import record_event

logger = logging.getLogger(__name__)

CONNECTOR_ID = "postgresql"

# ---------------------------------------------------------------------------
# Column alias maps — same base as SQL Server ingestor (T2-S11-A)
# ---------------------------------------------------------------------------

_DATE_COL_ALIASES = [
    "created_date", "opened_at", "create_date", "CreatedDate",
    "creation_time", "open_date", "OpenedAt", "sys_created_on",
]
_SLA_COL_ALIASES = [
    "sla_breached", "breach_flag", "sla_breach", "SLABreached",
    "breach", "made_sla", "sla_met",
]
_STATUS_COL_ALIASES = [
    "status", "Status", "state", "State", "incident_state",
    "ticket_status", "TicketStatus",
]
_PRIORITY_COL_ALIASES = [
    "priority", "Priority", "urgency", "Urgency",
    "severity", "Severity", "impact", "Impact",
]

# ---------------------------------------------------------------------------
# Signal queries — SELECT-only, schema-qualified, "" quoted identifiers,
# PostgreSQL date arithmetic.
#
# Boolean handling note (AC6):
# SLA_BREACH_QUERY uses native boolean (= TRUE).
# If the column is integer (0/1), psycopg2 raises a datatype/undefined-function
# error at execution time. The ingestor catches AgentIQ DBConnectionError or
# psycopg2 pgcode 42804/42883 and reruns the SLA query with = 1.
# ---------------------------------------------------------------------------

TICKET_VOLUME_QUERY = """
SELECT "{date_col}"::date AS ticket_date,
       COUNT(*) AS ticket_count
FROM "{schema}"."{table}"
WHERE "{date_col}" >= NOW() - INTERVAL '90 days'
GROUP BY "{date_col}"::date
ORDER BY ticket_date DESC
"""

SLA_BREACH_QUERY = """
SELECT COUNT(*) AS total_tickets,
       SUM(CASE WHEN "{sla_col}" = TRUE THEN 1 ELSE 0 END) AS breached_count,
       AVG(CASE WHEN "{sla_col}" = TRUE THEN 1.0 ELSE 0.0 END) * 100 AS breach_rate_pct
FROM "{schema}"."{table}"
WHERE "{date_col}" >= NOW() - INTERVAL '30 days'
"""

SLA_BREACH_INTEGER_FALLBACK_QUERY = """
SELECT COUNT(*) AS total_tickets,
       SUM(CASE WHEN "{sla_col}" = 1 THEN 1 ELSE 0 END) AS breached_count,
       AVG(CASE WHEN "{sla_col}" = 1 THEN 1.0 ELSE 0.0 END) * 100 AS breach_rate_pct
FROM "{schema}"."{table}"
WHERE "{date_col}" >= NOW() - INTERVAL '30 days'
"""

QUEUE_DEPTH_QUERY = """
SELECT "{priority_col}" AS priority,
       COUNT(*) AS queue_count,
       EXTRACT(EPOCH FROM AVG(NOW() - "{date_col}")) / 3600 AS avg_age_hours
FROM "{schema}"."{table}"
WHERE "{status_col}" NOT IN ('Closed', 'Resolved', 'Cancelled')
GROUP BY "{priority_col}"
ORDER BY "{priority_col}"
"""

# P1/P2 label set — covers numeric and named priority conventions
_P1_P2_LABELS = {"1", "p1", "2", "p2", "critical", "high"}
_BOOLEAN_FALLBACK_ERROR_CODES = {"query_error", "type_error"}
_BOOLEAN_FALLBACK_PG_CODES = {"42804", "42883"}  # datatype_mismatch, undefined_function

# ---------------------------------------------------------------------------
# Column resolution helpers
# ---------------------------------------------------------------------------

def _resolve_column(columns: list[str], aliases: list[str]) -> str | None:
    """Return first alias found in columns (case-insensitive), or None."""
    lower_cols = [c.lower() for c in columns]
    for alias in aliases:
        if alias.lower() in lower_cols:
            return columns[lower_cols.index(alias.lower())]
    return None


def _col_index(columns: list[str], name: str) -> int | None:
    """Return zero-based index of name in columns (case-insensitive), or None."""
    lower = [c.lower() for c in columns]
    try:
        return lower.index(name.lower())
    except ValueError:
        return None


def _iter_exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_boolean_fallback_error(exc: BaseException) -> bool:
    """Return True for native-boolean comparison failures that need = 1 retry."""
    for item in _iter_exception_chain(exc):
        if getattr(item, "error_code", None) in _BOOLEAN_FALLBACK_ERROR_CODES:
            return True
        if getattr(item, "pgcode", None) in _BOOLEAN_FALLBACK_PG_CODES:
            return True
        if item.__class__.__name__ in {"DatatypeMismatch", "UndefinedFunction"}:
            return True
    return False


def _is_query_timeout(exc: BaseException) -> bool:
    return any(
        getattr(item, "error_code", None) == "query_timeout"
        for item in _iter_exception_chain(exc)
    )


# ---------------------------------------------------------------------------
# Signal processors — each returns a signal dict; never raises
# ---------------------------------------------------------------------------

def _process_ticket_volume(result: Any) -> dict:
    """Build ticket_volume signal from TICKET_VOLUME_QUERY result."""
    date_idx = _col_index(result.columns, "ticket_date")
    count_idx = _col_index(result.columns, "ticket_count")

    if date_idx is None or count_idx is None:
        logger.warning(
            "postgresql_ingestor: ticket_volume — expected columns not found "
            "(columns=%s); setting degraded_signal=True",
            result.columns,
        )
        return {
            "daily_counts": [], "total_90d": 0, "avg_daily": 0.0,
            "peak_daily": 0, "peak_date": "", "recent_7d_avg": 0.0,
            "recent_vs_baseline": 0.0, "degraded_signal": True,
        }

    daily_counts = []
    for row in result.rows:
        date_val = row[date_idx]
        count_val = row[count_idx]
        daily_counts.append({
            "date": str(date_val) if date_val is not None else "",
            "count": int(count_val) if count_val is not None else 0,
        })

    if not daily_counts:
        return {
            "daily_counts": [], "total_90d": 0, "avg_daily": 0.0,
            "peak_daily": 0, "peak_date": "", "recent_7d_avg": 0.0,
            "recent_vs_baseline": 0.0, "degraded_signal": False,
        }

    total = sum(d["count"] for d in daily_counts)
    avg_daily = total / len(daily_counts)
    peak = max(daily_counts, key=lambda d: d["count"])
    recent_7d = daily_counts[:7]
    recent_7d_avg = (
        sum(d["count"] for d in recent_7d) / len(recent_7d) if recent_7d else 0.0
    )
    recent_vs_baseline = (recent_7d_avg / avg_daily) if avg_daily > 0 else 0.0

    return {
        "daily_counts": daily_counts,
        "total_90d": total,
        "avg_daily": round(avg_daily, 2),
        "peak_daily": peak["count"],
        "peak_date": peak["date"],
        "recent_7d_avg": round(recent_7d_avg, 2),
        "recent_vs_baseline": round(recent_vs_baseline, 4),
        "degraded_signal": False,
    }


def _process_sla_breach(result: Any) -> dict:
    """Build sla_breach signal from SLA_BREACH_QUERY result (native boolean)."""
    total_idx = _col_index(result.columns, "total_tickets")
    breached_idx = _col_index(result.columns, "breached_count")
    rate_idx = _col_index(result.columns, "breach_rate_pct")

    if total_idx is None or breached_idx is None:
        logger.warning(
            "postgresql_ingestor: sla_breach — expected columns not found "
            "(columns=%s); setting degraded_signal=True",
            result.columns,
        )
        return {
            "total_tickets_30d": 0, "breached_count": 0,
            "breach_rate_pct": 0.0, "degraded_signal": True,
        }

    row = result.rows[0] if result.rows else None
    if row is None:
        return {
            "total_tickets_30d": 0, "breached_count": 0,
            "breach_rate_pct": 0.0, "degraded_signal": False,
        }

    total = int(row[total_idx]) if row[total_idx] is not None else 0
    breached = int(row[breached_idx]) if row[breached_idx] is not None else 0
    if rate_idx is not None and row[rate_idx] is not None:
        rate = float(row[rate_idx])
    else:
        rate = (breached / total * 100.0) if total > 0 else 0.0

    return {
        "total_tickets_30d": total,
        "breached_count": breached,
        "breach_rate_pct": round(rate, 2),
        "degraded_signal": False,
    }


def _process_queue_depth(result: Any) -> dict:
    """Build queue_depth signal from QUEUE_DEPTH_QUERY result."""
    priority_idx = _col_index(result.columns, "priority")
    count_idx = _col_index(result.columns, "queue_count")
    age_idx = _col_index(result.columns, "avg_age_hours")

    if priority_idx is None or count_idx is None:
        logger.warning(
            "postgresql_ingestor: queue_depth — expected columns not found "
            "(columns=%s); setting degraded_signal=True",
            result.columns,
        )
        return {
            "by_priority": {}, "total_open": 0, "p1_p2_open": 0,
            "oldest_ticket_hours": 0.0, "degraded_signal": True,
        }

    by_priority: dict[str, Any] = {}
    total_open = 0
    oldest_hours = 0.0

    for row in result.rows:
        priority = str(row[priority_idx]) if row[priority_idx] is not None else "Unknown"
        count = int(row[count_idx]) if row[count_idx] is not None else 0
        avg_age = (
            float(row[age_idx])
            if (age_idx is not None and row[age_idx] is not None)
            else 0.0
        )
        by_priority[priority] = {"count": count, "avg_age_hours": round(avg_age, 1)}
        total_open += count
        if avg_age > oldest_hours:
            oldest_hours = avg_age

    p1_p2 = sum(
        v["count"]
        for k, v in by_priority.items()
        if k.strip().lower() in _P1_P2_LABELS
    )

    return {
        "by_priority": by_priority,
        "total_open": total_open,
        "p1_p2_open": p1_p2,
        "oldest_ticket_hours": round(oldest_hours, 1),
        "degraded_signal": False,
    }


# ---------------------------------------------------------------------------
# Degraded signal factories
# ---------------------------------------------------------------------------

def _degraded_ticket_volume() -> dict:
    return {
        "daily_counts": [], "total_90d": 0, "avg_daily": 0.0,
        "peak_daily": 0, "peak_date": "", "recent_7d_avg": 0.0,
        "recent_vs_baseline": 0.0, "degraded_signal": True,
    }


def _degraded_sla_breach() -> dict:
    return {
        "total_tickets_30d": 0, "breached_count": 0,
        "breach_rate_pct": 0.0, "degraded_signal": True,
    }


def _degraded_queue_depth() -> dict:
    return {
        "by_priority": {}, "total_open": 0, "p1_p2_open": 0,
        "oldest_ticket_hours": 0.0, "degraded_signal": True,
    }


# ---------------------------------------------------------------------------
# Main ingestor
# ---------------------------------------------------------------------------

def ingest(
    org_id: str,
    run_id: str,
    config: DBConnectorConfig,
    scope: ScopeDeclaration | None = None,
) -> dict:
    """PostgreSQL operational signal ingestor.

    Called by the runner for every discovery run where postgresql is in the
    selected systems list. Returns a structured dict consumed by the
    sqlserver_opsignal detector set (signal_source='postgresql').

    All data access goes through execute_query() — no direct connection opened.
    Tolerates query failures: sets degraded_signal=True on the affected metric
    and continues with remaining queries.

    Boolean fallback (AC6): SLA query uses native boolean (= TRUE).
    If the column is an integer type, execute_query may raise AgentIQ's
    DBConnectionError or a psycopg2 exception with pgcode 42804/42883.
    The ingestor retries with integer fallback (= 1) and logs a warning.
    """
    start_ms = time.monotonic()

    # Resolve scope; fall back to a minimal default for offline/test mode
    if scope is None:
        try:
            scope = get_scope(org_id, CONNECTOR_ID)
        except Exception:
            from datetime import datetime, timezone
            scope = ScopeDeclaration(
                org_id=org_id,
                connector_id=CONNECTOR_ID,
                schemas=["public"],
                tables=["public.service_tickets"],
                declared_at=datetime.now(timezone.utc),
                declared_by="system",
            )

    schema_name = scope.schemas[0] if scope.schemas else "public"
    raw_table = scope.tables[0] if scope.tables else f"{schema_name}.service_tickets"
    # Strip schema prefix — queries add it via "{schema}"."{table}" template
    table_name = raw_table.split(".", 1)[1] if "." in raw_table else raw_table

    query_count = 0
    degraded_count = 0

    # ── Query 1: Ticket volume trend (last 90 days) ──────────────────────────
    ticket_volume = _degraded_ticket_volume()
    try:
        q1 = TICKET_VOLUME_QUERY.format(
            date_col="created_date", schema=schema_name, table=table_name
        ).strip()
        result1 = execute_query(
            config, scope=scope, query=q1, org_id=org_id, run_id=run_id
        )
        query_count += 1
        ticket_volume = _process_ticket_volume(result1)
        if ticket_volume.get("degraded_signal"):
            degraded_count += 1
    except DBConnectionError as exc:
        if getattr(exc, "error_code", None) == "query_timeout":
            logger.warning(
                "postgresql_ingestor: ticket_volume query timed out — degraded_signal=True"
            )
        else:
            logger.warning("postgresql_ingestor: ticket_volume query failed: %s", exc)
        ticket_volume = _degraded_ticket_volume()
        degraded_count += 1
    except Exception as exc:
        logger.warning("postgresql_ingestor: ticket_volume unexpected error: %s", exc)
        ticket_volume = _degraded_ticket_volume()
        degraded_count += 1

    # ── Query 2: SLA breach rate (last 30 days) — native boolean, int fallback ─
    sla_breach = _degraded_sla_breach()
    try:
        q2 = SLA_BREACH_QUERY.format(
            sla_col="sla_breached", date_col="created_date",
            schema=schema_name, table=table_name
        ).strip()
        result2 = execute_query(
            config, scope=scope, query=q2, org_id=org_id, run_id=run_id
        )
        query_count += 1
        sla_breach = _process_sla_breach(result2)
        if sla_breach.get("degraded_signal"):
            degraded_count += 1
    except DBConnectionError as exc:
        if _is_boolean_fallback_error(exc):
            # Boolean column is actually integer — retry with integer fallback
            logger.warning(
                "postgresql_ingestor: sla_breach boolean type error — "
                "retrying with integer fallback (sla_breached = 1)"
            )
            try:
                q2_fallback = SLA_BREACH_INTEGER_FALLBACK_QUERY.format(
                    sla_col="sla_breached", date_col="created_date",
                    schema=schema_name, table=table_name
                ).strip()
                result2_fb = execute_query(
                    config, scope=scope, query=q2_fallback,
                    org_id=org_id, run_id=run_id
                )
                query_count += 1
                sla_breach = _process_sla_breach(result2_fb)
                if sla_breach.get("degraded_signal"):
                    degraded_count += 1
            except Exception as fb_exc:
                logger.warning(
                    "postgresql_ingestor: sla_breach integer fallback also failed: %s",
                    fb_exc,
                )
                sla_breach = _degraded_sla_breach()
                degraded_count += 1
        elif _is_query_timeout(exc):
            logger.warning(
                "postgresql_ingestor: sla_breach query timed out — degraded_signal=True"
            )
            sla_breach = _degraded_sla_breach()
            degraded_count += 1
        else:
            logger.warning("postgresql_ingestor: sla_breach query failed: %s", exc)
            sla_breach = _degraded_sla_breach()
            degraded_count += 1
    except Exception as exc:
        if _is_boolean_fallback_error(exc):
            logger.warning(
                "postgresql_ingestor: sla_breach psycopg2 boolean type error â€” "
                "retrying with integer fallback (sla_breached = 1)"
            )
            try:
                q2_fallback = SLA_BREACH_INTEGER_FALLBACK_QUERY.format(
                    sla_col="sla_breached", date_col="created_date",
                    schema=schema_name, table=table_name
                ).strip()
                result2_fb = execute_query(
                    config, scope=scope, query=q2_fallback,
                    org_id=org_id, run_id=run_id
                )
                query_count += 1
                sla_breach = _process_sla_breach(result2_fb)
                if sla_breach.get("degraded_signal"):
                    degraded_count += 1
            except Exception as fb_exc:
                logger.warning(
                    "postgresql_ingestor: sla_breach integer fallback also failed: %s",
                    fb_exc,
                )
                sla_breach = _degraded_sla_breach()
                degraded_count += 1
        else:
            logger.warning("postgresql_ingestor: sla_breach unexpected error: %s", exc)
            sla_breach = _degraded_sla_breach()
            degraded_count += 1

    # ── Query 3: Open queue depth by priority ────────────────────────────────
    queue_depth = _degraded_queue_depth()
    try:
        q3 = QUEUE_DEPTH_QUERY.format(
            priority_col="priority", status_col="status", date_col="created_date",
            schema=schema_name, table=table_name
        ).strip()
        result3 = execute_query(
            config, scope=scope, query=q3, org_id=org_id, run_id=run_id
        )
        query_count += 1
        queue_depth = _process_queue_depth(result3)
        if queue_depth.get("degraded_signal"):
            degraded_count += 1
    except DBConnectionError as exc:
        if getattr(exc, "error_code", None) == "query_timeout":
            logger.warning(
                "postgresql_ingestor: queue_depth query timed out — degraded_signal=True"
            )
        else:
            logger.warning("postgresql_ingestor: queue_depth query failed: %s", exc)
        queue_depth = _degraded_queue_depth()
        degraded_count += 1
    except Exception as exc:
        logger.warning("postgresql_ingestor: queue_depth unexpected error: %s", exc)
        queue_depth = _degraded_queue_depth()
        degraded_count += 1

    signal_count = (
        len(ticket_volume.get("daily_counts", []))
        + (1 if not sla_breach.get("degraded_signal") else 0)
        + len(queue_depth.get("by_priority", {}))
    )
    duration_ms = max(0, int((time.monotonic() - start_ms) * 1000))

    try:
        record_event(
            "db.ingestor_completed",
            {
                "connector_id": CONNECTOR_ID,
                "pack_id": "sqlserver_opsignal",
                "query_count": query_count,
                "signal_count": signal_count,
                "degraded_count": degraded_count,
                "duration_ms": duration_ms,
            },
        )
    except Exception:
        pass

    return {
        "ticket_volume": ticket_volume,
        "sla_breach": sla_breach,
        "queue_depth": queue_depth,
        "connector_id": CONNECTOR_ID,
        "org_id": org_id,
        "run_id": run_id,
        "schema_name": schema_name,
        "table_name": table_name,
    }
