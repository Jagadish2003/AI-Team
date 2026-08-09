# AgentIQ — PostgreSQL provisioning (dedicated server)

Scripts to stand up the complete AgentIQ schema on a dedicated PostgreSQL
server. Two interchangeable paths are provided (pick one):

- **Path A — Alembic + Python runbook** (maintained, recommended). Keeps Alembic
  as the single source of truth, stamps `alembic_version` so future migrations
  apply cleanly, and never drifts from the code.
- **Path B — Pure SQL bundle** (`psql` only). No Python or repo checkout needed
  on the DB box. It is a point-in-time snapshot of Path A's result, consolidated
  into a single [`provision.sql`](provision.sql) — regenerate it whenever
  migrations change (see *Regenerating the SQL bundle*).

Both produce the **same 54-table schema** plus the core reference seed
(connectors, mappings, permissions, uploads).

## Why the schema comes from more than just migrations

The AgentIQ schema is assembled from three sources. Any provisioning that runs
only `alembic upgrade head` will be **missing tables** — that was the original
defect this bundle fixes. The three sources:

| Source | Tables |
|---|---|
| Alembic migrations (`backend/migrations`) | Versioned native tables, including A1 projection instances, A2 lifecycle/baseline/movement records, A3 feedback/ranking state and history (+ `alembic_version`) |
| `seed_loader.py` (`{id,payload}` tables) | `connectors`, `uploads`, `runs`, `evidence`, `mappings`, `permissions`, `opportunities`, `audit_events`, `executive_reports`, `run_events`, `kv` |
| Lazy runtime creators (materialised up front by these scripts) | `credentials`, `nonces`, `oauth_nonces` |

Path A runs all three. Path B captures the union as flat SQL.

---

## Prerequisites (both paths)

1. A reachable PostgreSQL server (this app is developed against **PostgreSQL 17**).
2. The application **database** must already exist. Create it once as a superuser
   (e.g. `postgres`), connected to the maintenance DB `postgres`:

   ```bash
   psql -h <DB_HOST> -p 5432 -U postgres -d postgres -c "CREATE DATABASE agentiq;"
   ```

   The application **role** (`agentiq`) is created automatically:
   - Path B — by [`provision.sql`](provision.sql) (idempotent `CREATE ROLE`).
   - Path A — supply an existing role in `DATABASE_URL` (Alembic does not create
     roles); create it first if needed with
     `psql ... -c "CREATE ROLE agentiq LOGIN PASSWORD '<password>';"`.

   Change the role password (in `provision.sql` for Path B) before running on a
   shared/production server.

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
1. Materialises the `{id,payload}` core tables without seed rows, so migrations
   can extend/protect `runs` and `kv` on a clean install.
2. Runs `alembic upgrade head` for native tables + version stamp.
3. Optionally upserts the core reference seed (`--no-seed` skips these rows).
4. Materialises the lazy-only tables (`credentials`, `nonces`, `oauth_nonces`).
5. Re-applies history privileges, then verifies the A1/A2/A3 tables, keys,
   indexes, migration head, and application-role privileges.

You can also call the underlying script directly:
`python database/provision/provision_schema.py [--no-seed]`.

---

## Path B — Pure SQL bundle (`psql` only)

No Python needed. After the prerequisite database exists, run the single
[`provision.sql`](provision.sql) against an **empty** target database as a
superuser (or the schema owner) — it creates the `agentiq` role and assigns
schema ownership, then creates all tables and seeds the core reference rows:

```bash
export PGPASSWORD='<password>'
psql -h <DB_HOST> -p 5432 -U postgres -d <DB_NAME> -v ON_ERROR_STOP=1 -f provision.sql
```

- [`provision.sql`](provision.sql) — creates the `agentiq` role + schema grants,
  then all 54 tables (including `org_licenses`, `license_registry`,
  `issuance_audit`), indexes, sequences, the core
  reference seed (connectors, mappings, permissions, uploads) as idempotent
  `INSERT … ON CONFLICT DO NOTHING`, and the `alembic_version` stamp (so a later
  `alembic upgrade head` applies only *new* migrations). No run/telemetry/audit
  data.

> `provision.sql` fully schema-qualifies every object and uses plain `INSERT`s
> (not `COPY … FROM stdin`) and no `psql` meta-commands, so it runs under both
> `psql` and any SQL client. Restore into a fresh database, not an existing one
> with conflicting objects — `CREATE TABLE` statements are not `IF NOT EXISTS`.

---

## Verification (either path)

```sql
SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';  -- 54
SELECT version_num FROM alembic_version;                                       -- 0049
SELECT count(*) FROM connectors;                                               -- > 0 if seeded
```

For the full A1 -> A2 -> A3 contract, run the read-only checker:

```bash
cd backend
python database/provision/a1_a3_readiness.py --target all
```

It reads `DEV_DATABASE_URL` and `PROD_DATABASE_URL`, prints no connection string
or password, and exits non-zero if either database cannot store projections,
track outcomes, learn from them, or preserve the related history.

Then point the backend at `DATABASE_URL` and start it. On first start the app
seeds the dev/owner workspace member and creates anything still missing.

---

## Regenerating the SQL bundle (keep Path B in sync)

`provision.sql` is a snapshot of Path A. After adding/altering migrations,
regenerate it from a database that has just been provisioned via Path A (so it is
exactly at head). With `pg_dump`/`psql` on `PATH` and `DATABASE_URL` exported:

```bash
cd backend
# derive PG* connection vars from DATABASE_URL, then dump schema + seed into ONE file.
# --inserts --on-conflict-do-nothing keeps the bundle psql- and client-portable
# (no COPY FROM stdin); -t limits data to the core reference tables.
pg_dump --no-owner --no-privileges --no-comments --quote-all-identifiers \
        --inserts --on-conflict-do-nothing \
        --schema=public \
        -t connectors -t mappings -t permissions -t uploads \
        "$PGDATABASE" > /tmp/_data.sql        # seed rows only

pg_dump --schema-only --no-owner --no-privileges --no-comments \
        --quote-all-identifiers "$PGDATABASE" > database/provision/provision.sql

HEAD=$(psql -tAc "SELECT version_num FROM alembic_version")
cat /tmp/_data.sql >> database/provision/provision.sql
printf '\nINSERT INTO "public"."alembic_version" ("version_num") VALUES ('\''%s'\'') ON CONFLICT DO NOTHING;\n' \
       "$HEAD" >> database/provision/provision.sql
```

> **After regenerating**, (1) strip any `\restrict` / `\unrestrict` psql
> meta-commands `pg_dump` emits (so the file runs under non-`psql` clients too),
> and (2) re-add the application-role block that `pg_dump` does not emit. Insert
> it just after the `SET` block, before the first `CREATE TABLE`:
>
> ```sql
> DO
> $$
> BEGIN
>     IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentiq') THEN
>         CREATE ROLE agentiq LOGIN PASSWORD 'agentiq';
>     END IF;
> END
> $$;
> ALTER SCHEMA public OWNER TO agentiq;
> GRANT ALL ON SCHEMA public TO agentiq;
> ```

---

## Notes

- **Performance / dedicated server.** Pointing `DATABASE_URL` at a dedicated
  PostgreSQL host is exactly the intended setup — nothing app-side changes; only
  the connection string differs from `localhost`.
- These scripts do **not** manage connector OAuth secrets, `CREDENTIAL_VAULT_KEY`,
  `JWT_SECRET`, etc. See [`deployment/README.md`](../../../deployment/README.md)
  for the runtime env vars required in production.
- Re-running Path A is safe. Re-running Path B requires an empty database.
