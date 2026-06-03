# Native DB Connectors

## Overview

AgentIQ supports direct connections to enterprise databases via native DB connectors. Supported drivers: **Oracle**, **PostgreSQL**, **SQL Server**.

Source: `backend/connectors/db/`

---

## Architecture

```
routes_db_connectors.py  ←  registers FastAPI routes
  └── db_connectors/      ←  API models and route handlers
  └── connectors/db/      ←  driver implementations
        ├── oracle.py
        ├── postgres.py
        ├── sqlserver.py
        ├── query_guard.py   ← SQL injection prevention (mandatory)
        ├── scope.py         ← table/column scope management
        └── connection_pool.py
```

---

## SQL Injection Prevention

**`query_guard.py` must be invoked for every native DB query.** Skipping it bypasses injection protection silently.

```python
from connectors.db.query_guard import guard_query

safe_sql = guard_query(user_provided_sql, allowed_tables=scope.tables)
```

The guard validates that the query only touches tables and columns in the allowed scope. Queries referencing out-of-scope objects are rejected with a `QueryScopeError`.

---

## Driver Installation

System-level DB drivers must be installed separately. See `deployment/README.md` for OS-specific install instructions for Oracle Instant Client, `pyodbc` (SQL Server), and `psycopg2` (PostgreSQL).

---

## Scope Management (`scope.py`)

Each connector connection has a declared scope — the set of tables and columns it is allowed to query. Scope is defined in the connector configuration and enforced by `query_guard.py`.

---

## Tests

```powershell
cd backend
python -m pytest connectors\db\tests
```

Tests include smoke tests for each driver. Live driver tests require a running database instance and the corresponding env vars set.
