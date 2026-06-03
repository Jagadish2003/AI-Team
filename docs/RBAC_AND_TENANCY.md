# RBAC and Multi-Tenancy

## Overview

AgentIQ enforces multi-tenancy and role-based access control (RBAC) on every API request. These two systems work together: tenancy scopes data to an organisation, and RBAC gates operations within that organisation.

---

## Multi-Tenancy (`middleware/tenancy.py`)

Every request is scoped to an **org**. The middleware extracts `org_id` from the bearer token and attaches it to the request state. All DB reads that use `tenancy_get_all` / `tenancy_get_one` automatically filter by `org_id`.

- Default local org: `default`
- Override via `X-Org-Id` header in tests or multi-tenant scenarios

---

## RBAC (`rbac.py`)

Three roles are supported:

| Role | Permissions |
|---|---|
| `owner` | Full access — all read and write operations, audit log, workspace members |
| `analyst` | Read + decision/override writes on opportunities and evidence |
| `viewer` | Read-only access to connectors, opportunities, roadmap |

### Gating a route

```python
from .rbac import require_role

@app.get("/api/some-route", dependencies=[Depends(require_auth), Depends(require_role("analyst"))])
def my_route():
    ...
```

### Dev setup

On startup, `seed_owner(org_id, dev_user)` promotes the dev token to `owner`. The role can be overridden for testing via `DEV_JWT_ROLE`:

```
DEV_JWT_ROLE=viewer
```

Test tokens for each role are available in contract tests via fixtures: `ADMIN_JWT`, `ANALYST_JWT`, `VIEWER_JWT`.

---

## Audit Trail (`middleware/audit.py`)

All mutating requests (POST, PUT, PATCH, DELETE) are automatically logged to the `audit_log` table. Entries are org-scoped and retrievable via `GET /api/audit-log` (owner only).

---

## Adding a New Connector Secret

Connector OAuth secrets must follow the naming convention:

```
{CONNECTOR_NAME}_CLIENT_SECRET
```

where `CONNECTOR_NAME` is uppercase (e.g., `SALESFORCE_CLIENT_SECRET`, `JIRA_CLIENT_SECRET`). This convention is enforced by `auth/secrets.py` and validated on startup when `REQUIRE_CONNECTOR_SECRETS=1`.

See `backend/app/auth/README.md` for the full connector auth framework guide.
