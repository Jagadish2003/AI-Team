# AgentIQ — PostgreSQL provisioning (dedicated server)

Scripts to stand up the complete AgentIQ schema on a dedicated PostgreSQL
server. Two interchangeable paths are provided (pick one):

- **Path A — Alembic + Python runbook** (maintained, recommended). Keeps Alembic
  as the single source of truth, stamps `alembic_version` so future migrations
  apply cleanly, and never drifts from the code.
- **Path B — Pure SQL bundle** (`psql` only). No Python or repo checkout needed
  on the DB box. It is a point-in-time snapshot of Path A's result — regenerate
  it whenever migrations change (see *Regenerating the SQL bundle*).

Both produce the **same 25-table schema** plus the core reference seed
(connectors, mappings, permissions, uploads).

## Why the schema comes from more than just migrations

The AgentIQ schema is assembled from three sources. Any provisioning that runs
only `alembic upgrade head` will be **missing tables** — that was the original
defect this bundle fixes. The three sources:

| Source | Tables |
|---|---|
| Alembic migrations (`backend/migrations`) | `telemetry_events`, `signal_snapshots`, `entities`, `users`, `login_attempts`, `orgs`, `entity_relationships`, `causal_hypotheses`, `audit_log`, `workspace_members` (+ `alembic_version`) |
| `seed_loader.py` (`{id,payload}` tables) | `connectors`, `uploads`, `runs`, `evidence`, `mappings`, `permissions`, `opportunities`, `audit_events`, `executive_reports`, `run_events`, `kv` |
| Lazy runtime creators (materialised up front by these scripts) | `credentials`, `nonces`, `oauth_nonces` |

Path A runs all three. Path B captures the union as flat SQL.

---

## Prerequisites (both paths)

1. A reachable PostgreSQL server (this app is developed against **PostgreSQL 17**).
2. The application role + database. Run **once** as a superuser (e.g. `postgres`),
   connected to the maintenance DB `postgres`:

   ```bash
   psql -h <DB_HOST> -p 5432 -U postgres -d postgres -f 00_create_role_and_db.sql
   ```

   Edit the password in [`00_create_role_and_db.sql`](00_create_role_and_db.sql)
   before running it on a shared/production server.

3. The connection string the backend will use:

   ```
   DATABASE_URL=postgresql://agentiq:<password>@<DB_HOST>:5432/agentiq
   ```

   For TLS to the dedicated server, append `?sslmode=require` (or `verify-full`
   with a CA), e.g. `...:5432/agentiq?sslmode=require`.

---

## Path A — Alembic + Python runbook (recommended)

Run from a machine that has the repo + the backend venv and can reach the DB.

```bash
cd backend
# (venv active; deps installed: pip install -r requirements.txt)

export DATABASE_URL="postgresql://agentiq:<password>@<DB_HOST>:5432/agentiq"
./database/provision/provision.sh                 # schema + core seed
# ./database/provision/provision.sh --no-seed      # schema only
```

PowerShell:

```powershell
cd backend
$env:DATABASE_URL = "postgresql://agentiq:<password>@<DB_HOST>:5432/agentiq"
.\database\provision\provision.ps1                 # schema + core seed
# .\database\provision\provision.ps1 -NoSeed        # schema only
```

What it does (idempotent, never drops anything):
1. `alembic upgrade head` — native tables + version stamp.
2. `seed_loader.py --no-reset` — `{id,payload}` tables + core reference seed
   (`--no-seed` creates the tables only).
3. Materialises the lazy-only tables (`credentials`, `nonces`, `oauth_nonces`).
4. Prints the resulting table inventory and `alembic_version`.

You can also call the underlying script directly:
`python database/provision/provision_schema.py [--no-seed]`.

---

## Path B — Pure SQL bundle (`psql` only)

No Python needed. After the prerequisite role/DB exist, against an **empty**
`agentiq` database:

```bash
export PGPASSWORD='<password>'
psql -h <DB_HOST> -p 5432 -U agentiq -d agentiq -v ON_ERROR_STOP=1 -f 01_schema.sql
psql -h <DB_HOST> -p 5432 -U agentiq -d agentiq -v ON_ERROR_STOP=1 -f 02_seed.sql
```

- [`01_schema.sql`](01_schema.sql) — all 25 tables, indexes, sequences, and the
  `alembic_version` stamp (so a later `alembic upgrade head` correctly applies
  only *new* migrations).
- [`02_seed.sql`](02_seed.sql) — core reference rows only (connectors, mappings,
  permissions, uploads). No run/telemetry/audit data.

> The SQL files set `search_path = ''` and fully schema-qualify every object
> (a pg_dump safety default). Restore into a fresh database, not an existing one
> with conflicting objects — `CREATE TABLE` statements are not `IF NOT EXISTS`.

---

## Verification (either path)

```sql
SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';  -- 25
SELECT version_num FROM alembic_version;                                       -- current head
SELECT count(*) FROM connectors;                                               -- > 0 if seeded
```

Then point the backend at `DATABASE_URL` and start it. On first start the app
seeds the dev/owner workspace member and creates anything still missing.

---

## Regenerating the SQL bundle (keep Path B in sync)

Path B is a snapshot of Path A. After adding/altering migrations, regenerate it
from a database that has just been provisioned via Path A (so it is exactly at
head). With `pg_dump`/`psql` v17 on `PATH` and `DATABASE_URL` exported:

```bash
cd backend
# derive PG* connection vars from DATABASE_URL, then:
pg_dump --schema-only --no-owner --no-privileges --no-comments \
        --quote-all-identifiers "$PGDATABASE" > database/provision/01_schema.sql
# append the version stamp (schema-qualified — search_path is '' in the dump):
HEAD=$(psql -tAc "SELECT version_num FROM alembic_version")
printf '\n-- Alembic version stamp (snapshot at head %s)\n' "$HEAD" >> database/provision/01_schema.sql
printf 'INSERT INTO "public"."alembic_version" ("version_num") VALUES ('\''%s'\'') ON CONFLICT DO NOTHING;\n' "$HEAD" >> database/provision/01_schema.sql

pg_dump --data-only --no-owner --no-privileges --quote-all-identifiers \
        -t connectors -t mappings -t permissions -t uploads \
        "$PGDATABASE" > database/provision/02_seed.sql
```

---

## Notes

- **Performance / dedicated server.** Pointing `DATABASE_URL` at a dedicated
  PostgreSQL host is exactly the intended setup — nothing app-side changes; only
  the connection string differs from `localhost`.
- These scripts do **not** manage connector OAuth secrets, `CREDENTIAL_VAULT_KEY`,
  `JWT_SECRET`, etc. See [`deployment/README.md`](../../../deployment/README.md)
  for the runtime env vars required in production.
- Re-running Path A is safe. Re-running Path B requires an empty database.
