"""
PostgreSQL connection driver - T2-S10-A Task T5.

Driver: psycopg2-binary.
SSL:    config.ssl_mode or POSTGRESQL_SSL_MODE, limited to require/prefer/disable.
Timeouts:
  connect_timeout_s -> psycopg2.connect(connect_timeout=...)
  query_timeout_s   -> psycopg2 connection options statement_timeout in ms.

Quoted identifiers: use double quotes for schema and table identifiers.
Registers itself with the pool factory at import time via register_driver().
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import psycopg2
from psycopg2.extensions import connection as PsycopgConnection

from backend.app.db_connectors.models import (
    ColumnMeta,
    DBConnectionError,
    DBConnectorConfig,
    ScopeDeclaration,
    SchemaDiscoveryResult,
    TableMeta,
)
from backend.connectors.db.connection_pool import register_driver

logger = logging.getLogger(__name__)

CONNECTOR_ID: str = "postgresql"
PACK_ID: str = "sqlserver_opsignal"


SSL_MODE_ENV_VAR: str = "POSTGRESQL_SSL_MODE"
ALLOWED_SSL_MODES: frozenset[str] = frozenset({"require", "prefer", "disable"})

TRIVIAL_QUERY: str = "SELECT 1"

CATALOGUE_QUERY: str = (
    "SELECT table_schema, table_name, column_name "
    "FROM information_schema.columns "
    "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
)

# ---------------------------------------------------------------------------
# Sprint 12 operational signal queries
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

_DATE_COL_ALIASES = [
    "created_date", "opened_at", "create_date", "creation_time",
    "open_date", "sys_created_on",
]
_SLA_COL_ALIASES = [
    "sla_breached", "breach_flag", "sla_breach", "breach",
    "made_sla", "sla_met",
]
_STATUS_COL_ALIASES = [
    "status", "state", "incident_state", "ticket_status",
]
_PRIORITY_COL_ALIASES = [
    "priority", "urgency", "severity", "impact",
]
COLUMN_ALIASES: dict[str, list[str]] = {
    "date": _DATE_COL_ALIASES,
    "sla": _SLA_COL_ALIASES,
    "status": _STATUS_COL_ALIASES,
    "priority": _PRIORITY_COL_ALIASES,
}
_P1_P2_LABELS = {"1", "p1", "2", "p2", "critical", "high"}


def quote_identifier(name: str) -> str:
    """Wrap a PostgreSQL identifier in double quotes."""
    return '"' + name.replace('"', '""') + '"'


def qualified_table_name(schema: str, table: str) -> str:
    """Return a schema-qualified table reference with both identifiers quoted."""
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def _resolve_ssl_mode(config: DBConnectorConfig) -> str:
    """Resolve and validate the PostgreSQL sslmode value."""
    raw_mode = config.ssl_mode or os.environ.get(SSL_MODE_ENV_VAR, "prefer")
    ssl_mode = raw_mode.strip().lower()
    if ssl_mode not in ALLOWED_SSL_MODES:
        allowed = ", ".join(sorted(ALLOWED_SSL_MODES))
        raise DBConnectionError(
            f"Invalid PostgreSQL ssl_mode {ssl_mode!r}. Allowed values: {allowed}.",
            error_code="invalid_ssl_mode",
        )
    return ssl_mode


def _statement_timeout_option(config: DBConnectorConfig) -> str:
    timeout_ms = int(config.query_timeout_s) * 1000
    return f"-c statement_timeout={timeout_ms}"


def _connect_kwargs(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Build keyword arguments for psycopg2.connect().

    The returned dict contains credentials and must never be logged.
    """
    return {
        "host": config.host,
        "port": config.port,
        "dbname": config.database,
        "user": username,
        "password": password,
        "connect_timeout": config.connect_timeout_s,
        "sslmode": _resolve_ssl_mode(config),
        "options": _statement_timeout_option(config),
    }


def create_postgresql_connection(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> PsycopgConnection:
    """Create and return a psycopg2 connection to PostgreSQL."""
    kwargs = _connect_kwargs(config, username, password)
    try:
        return psycopg2.connect(**kwargs)
    except DBConnectionError:
        raise
    except psycopg2.OperationalError as exc:
        raise DBConnectionError(
            f"PostgreSQL connection failed [{type(exc).__name__}] "
            f"host={config.host!r} db={config.database!r}",
            error_code="connection_failed",
        ) from exc
    except psycopg2.Error as exc:
        raise DBConnectionError(
            f"PostgreSQL driver error [{type(exc).__name__}] "
            f"host={config.host!r} db={config.database!r}",
            error_code="connection_failed",
        ) from exc
    except Exception as exc:
        raise DBConnectionError(
            f"Unexpected error connecting to PostgreSQL "
            f"host={config.host!r} db={config.database!r}: {type(exc).__name__}",
            error_code="connection_failed",
        ) from exc


def discover_schema_postgresql(conn: PsycopgConnection) -> SchemaDiscoveryResult:
    """Run the information_schema catalogue query and return schema metadata."""
    cursor = conn.cursor()
    try:
        cursor.execute(CATALOGUE_QUERY)
        rows = cursor.fetchall()
    finally:
        cursor.close()

    schemas: set[str] = set()
    tables_seen: set[tuple[str, str]] = set()
    columns: list[ColumnMeta] = []

    for schema_name, table_name, column_name in rows:
        schemas.add(schema_name)
        tables_seen.add((schema_name, table_name))
        columns.append(
            ColumnMeta(schema=schema_name, table=table_name, column=column_name)
        )

    return SchemaDiscoveryResult(
        schemas=sorted(schemas),
        tables=[TableMeta(schema=s, table=t) for s, t in sorted(tables_seen)],
        columns=columns,
        estimated_row_counts=None,
    )


def _execute_query(
    config: DBConnectorConfig,
    *,
    scope: ScopeDeclaration,
    query: str,
    org_id: str,
    run_id: str,
) -> Any:
    """Lazy execute_query import avoids circular imports with this driver module."""
    try:
        from backend.connectors.db import execute_query
    except ModuleNotFoundError:
        from connectors.db import execute_query
    return execute_query(config, scope=scope, query=query, org_id=org_id, run_id=run_id)


def _get_scope(org_id: str) -> ScopeDeclaration:
    try:
        from backend.connectors.db import get_scope
    except ModuleNotFoundError:
        from connectors.db import get_scope
    return get_scope(org_id, CONNECTOR_ID)


def _record_event(event_type: str, payload: dict[str, Any]) -> None:
    try:
        from backend.app.telemetry import record_event
    except ModuleNotFoundError:
        from app.telemetry import record_event
    record_event(event_type, payload)


def _col_index(columns: list[str], name: str) -> int | None:
    lower = [c.lower() for c in columns]
    try:
        return lower.index(name.lower())
    except ValueError:
        return None


def _process_ticket_volume(result: Any) -> dict[str, Any]:
    date_idx = _col_index(result.columns, "ticket_date")
    count_idx = _col_index(result.columns, "ticket_count")

    if date_idx is None or count_idx is None:
        logger.warning(
            "postgresql_ingestor: ticket_volume expected columns not found "
            "(columns=%s); setting degraded_signal=True",
            result.columns,
        )
        return _degraded_ticket_volume()

    daily_counts: list[dict[str, Any]] = []
    for row in result.rows:
        date_val = row[date_idx]
        count_val = row[count_idx]
        daily_counts.append(
            {
                "date": str(date_val) if date_val is not None else "",
                "count": int(count_val) if count_val is not None else 0,
            }
        )

    if not daily_counts:
        return {
            "daily_counts": [],
            "total_90d": 0,
            "avg_daily": 0.0,
            "peak_daily": 0,
            "peak_date": "",
            "recent_7d_avg": 0.0,
            "recent_vs_baseline": 0.0,
            "degraded_signal": False,
        }

    total = sum(d["count"] for d in daily_counts)
    avg_daily = total / len(daily_counts)
    peak = max(daily_counts, key=lambda d: d["count"])
    recent_7d = daily_counts[:7]
    recent_7d_avg = (
        sum(d["count"] for d in recent_7d) / len(recent_7d) if recent_7d else 0.0
    )
    recent_vs_baseline = recent_7d_avg / avg_daily if avg_daily > 0 else 0.0

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


def _process_sla_breach(result: Any) -> dict[str, Any]:
    total_idx = _col_index(result.columns, "total_tickets")
    breached_idx = _col_index(result.columns, "breached_count")
    rate_idx = _col_index(result.columns, "breach_rate_pct")

    if total_idx is None or breached_idx is None:
        logger.warning(
            "postgresql_ingestor: sla_breach expected columns not found "
            "(columns=%s); setting degraded_signal=True",
            result.columns,
        )
        return _degraded_sla_breach()

    row = result.rows[0] if result.rows else None
    if row is None:
        return {
            "total_tickets_30d": 0,
            "breached_count": 0,
            "breach_rate_pct": 0.0,
            "degraded_signal": False,
        }

    total = int(row[total_idx]) if row[total_idx] is not None else 0
    breached = int(row[breached_idx]) if row[breached_idx] is not None else 0
    if rate_idx is not None and row[rate_idx] is not None:
        rate = float(row[rate_idx])
    else:
        rate = breached / total * 100.0 if total > 0 else 0.0

    return {
        "total_tickets_30d": total,
        "breached_count": breached,
        "breach_rate_pct": round(rate, 2),
        "degraded_signal": False,
    }


def _process_queue_depth(result: Any) -> dict[str, Any]:
    priority_idx = _col_index(result.columns, "priority")
    count_idx = _col_index(result.columns, "queue_count")
    age_idx = _col_index(result.columns, "avg_age_hours")

    if priority_idx is None or count_idx is None:
        logger.warning(
            "postgresql_ingestor: queue_depth expected columns not found "
            "(columns=%s); setting degraded_signal=True",
            result.columns,
        )
        return _degraded_queue_depth()

    by_priority: dict[str, Any] = {}
    total_open = 0
    oldest_hours = 0.0

    for row in result.rows:
        priority = str(row[priority_idx]) if row[priority_idx] is not None else "Unknown"
        count = int(row[count_idx]) if row[count_idx] is not None else 0
        avg_age = (
            float(row[age_idx])
            if age_idx is not None and row[age_idx] is not None
            else 0.0
        )
        by_priority[priority] = {"count": count, "avg_age_hours": round(avg_age, 1)}
        total_open += count
        oldest_hours = max(oldest_hours, avg_age)

    p1_p2_open = sum(
        value["count"]
        for priority, value in by_priority.items()
        if priority.strip().lower() in _P1_P2_LABELS
    )

    return {
        "by_priority": by_priority,
        "total_open": total_open,
        "p1_p2_open": p1_p2_open,
        "oldest_ticket_hours": round(oldest_hours, 1),
        "degraded_signal": False,
    }


def _degraded_ticket_volume() -> dict[str, Any]:
    return {
        "daily_counts": [],
        "total_90d": 0,
        "avg_daily": 0.0,
        "peak_daily": 0,
        "peak_date": "",
        "recent_7d_avg": 0.0,
        "recent_vs_baseline": 0.0,
        "degraded_signal": True,
    }


def _degraded_sla_breach() -> dict[str, Any]:
    return {
        "total_tickets_30d": 0,
        "breached_count": 0,
        "breach_rate_pct": 0.0,
        "degraded_signal": True,
    }


def _degraded_queue_depth() -> dict[str, Any]:
    return {
        "by_priority": {},
        "total_open": 0,
        "p1_p2_open": 0,
        "oldest_ticket_hours": 0.0,
        "degraded_signal": True,
    }


def _default_scope(org_id: str) -> ScopeDeclaration:
    from datetime import datetime, timezone

    return ScopeDeclaration(
        org_id=org_id,
        connector_id=CONNECTOR_ID,
        schemas=["public"],
        tables=["public.ServiceTickets"],
        declared_at=datetime.now(timezone.utc),
        declared_by="system",
    )


def _scope_table(scope: ScopeDeclaration) -> tuple[str, str]:
    schema_name = scope.schemas[0] if scope.schemas else "public"
    raw_table = scope.tables[0] if scope.tables else f"{schema_name}.ServiceTickets"
    table_name = raw_table.split(".", 1)[1] if "." in raw_table else raw_table
    return schema_name, table_name


def _is_query_timeout(exc: DBConnectionError) -> bool:
    return getattr(exc, "error_code", None) == "query_timeout"


def _should_retry_sla_integer_fallback(exc: DBConnectionError) -> bool:
    if _is_query_timeout(exc):
        return False

    text = f"{type(exc).__name__} {exc} {getattr(exc, 'error_code', '')}".lower()
    return any(
        marker in text
        for marker in (
            "operator",
            "undefinedfunction",
            "datatype",
            "type",
            "boolean",
            "integer",
            "query_failed",
        )
    )


def ingest(
    org_id: str,
    run_id: str,
    config: DBConnectorConfig,
    scope: ScopeDeclaration | None = None,
) -> dict[str, Any]:
    """PostgreSQL operational signal ingestor for Sprint 12.

    All data access goes through execute_query(); this ingestor never opens a
    direct psycopg2 connection. It returns the same shape as the SQL Server
    ingestor so the shared sqlserver_opsignal detectors can consume the data.
    """
    start_ms = time.monotonic()

    if scope is None:
        try:
            scope = _get_scope(org_id)
        except Exception:
            scope = _default_scope(org_id)

    schema_name, table_name = _scope_table(scope)
    query_count = 0
    degraded_count = 0

    ticket_volume = _degraded_ticket_volume()
    try:
        query_count += 1
        result = _execute_query(
            config,
            scope=scope,
            query=TICKET_VOLUME_QUERY.format(
                date_col=COLUMN_ALIASES["date"][0],
                schema=schema_name,
                table=table_name,
            ).strip(),
            org_id=org_id,
            run_id=run_id,
        )
        ticket_volume = _process_ticket_volume(result)
        degraded_count += 1 if ticket_volume.get("degraded_signal") else 0
    except DBConnectionError as exc:
        if _is_query_timeout(exc):
            logger.warning(
                "postgresql_ingestor: ticket_volume query timed out; "
                "degraded_signal=True"
            )
        else:
            logger.warning("postgresql_ingestor: ticket_volume query failed: %s", exc)
        ticket_volume = _degraded_ticket_volume()
        degraded_count += 1
    except Exception as exc:
        logger.warning("postgresql_ingestor: ticket_volume unexpected error: %s", exc)
        ticket_volume = _degraded_ticket_volume()
        degraded_count += 1

    sla_breach = _degraded_sla_breach()
    try:
        query_count += 1
        result = _execute_query(
            config,
            scope=scope,
            query=SLA_BREACH_QUERY.format(
                sla_col=COLUMN_ALIASES["sla"][0],
                date_col=COLUMN_ALIASES["date"][0],
                schema=schema_name,
                table=table_name,
            ).strip(),
            org_id=org_id,
            run_id=run_id,
        )
        sla_breach = _process_sla_breach(result)
        degraded_count += 1 if sla_breach.get("degraded_signal") else 0
    except DBConnectionError as exc:
        if _should_retry_sla_integer_fallback(exc):
            logger.warning(
                "postgresql_ingestor: sla_breach native boolean query failed; "
                "retrying integer fallback"
            )
            try:
                query_count += 1
                result = _execute_query(
                    config,
                    scope=scope,
                    query=SLA_BREACH_INTEGER_FALLBACK_QUERY.format(
                        sla_col=COLUMN_ALIASES["sla"][0],
                        date_col=COLUMN_ALIASES["date"][0],
                        schema=schema_name,
                        table=table_name,
                    ).strip(),
                    org_id=org_id,
                    run_id=run_id,
                )
                sla_breach = _process_sla_breach(result)
                degraded_count += 1 if sla_breach.get("degraded_signal") else 0
            except DBConnectionError as fallback_exc:
                if _is_query_timeout(fallback_exc):
                    logger.warning(
                        "postgresql_ingestor: sla_breach fallback query timed out; "
                        "degraded_signal=True"
                    )
                else:
                    logger.warning(
                        "postgresql_ingestor: sla_breach fallback query failed: %s",
                        fallback_exc,
                    )
                sla_breach = _degraded_sla_breach()
                degraded_count += 1
            except Exception as fallback_exc:
                logger.warning(
                    "postgresql_ingestor: sla_breach fallback unexpected error: %s",
                    fallback_exc,
                )
                sla_breach = _degraded_sla_breach()
                degraded_count += 1
        else:
            if _is_query_timeout(exc):
                logger.warning(
                    "postgresql_ingestor: sla_breach query timed out; "
                    "degraded_signal=True"
                )
            else:
                logger.warning("postgresql_ingestor: sla_breach query failed: %s", exc)
            sla_breach = _degraded_sla_breach()
            degraded_count += 1
    except Exception as exc:
        logger.warning("postgresql_ingestor: sla_breach unexpected error: %s", exc)
        sla_breach = _degraded_sla_breach()
        degraded_count += 1

    queue_depth = _degraded_queue_depth()
    try:
        query_count += 1
        result = _execute_query(
            config,
            scope=scope,
            query=QUEUE_DEPTH_QUERY.format(
                priority_col=COLUMN_ALIASES["priority"][0],
                status_col=COLUMN_ALIASES["status"][0],
                date_col=COLUMN_ALIASES["date"][0],
                schema=schema_name,
                table=table_name,
            ).strip(),
            org_id=org_id,
            run_id=run_id,
        )
        queue_depth = _process_queue_depth(result)
        degraded_count += 1 if queue_depth.get("degraded_signal") else 0
    except DBConnectionError as exc:
        if _is_query_timeout(exc):
            logger.warning(
                "postgresql_ingestor: queue_depth query timed out; "
                "degraded_signal=True"
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
        _record_event(
            "db.ingestor_completed",
            {
                "connector_id": CONNECTOR_ID,
                "pack_id": PACK_ID,
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


register_driver("postgresql", create_postgresql_connection)
