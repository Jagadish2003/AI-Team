# AgentIQ Deployment — Environment Variables

## Per-Instance Configuration (POC Model)

> **DEPLOYMENT NOTE — per-instance config (POC model)**
>
> Org-specific configuration lives **per deployment instance**, not in a shared
> store. Each customer (TCU, City National) runs on a **separate instance** with
> its own `config.py` and environment variables.
>
> **FUTURE:** when multi-tenant shared deployment is needed, these settings move
> to a `workspace_config` table keyed by `org_id`.
> See architectural discussion 2026-06-10 — workspace config externalisation.
> Do not remove this note until that story is built.

### Descope — Workspace config externalisation (per Team Lead)

**Descoped from the current sprint (Sprint 12); to be done after the POC.**

Both POCs (TCU, City National) run on **separate instances**, so each already
has its own isolated `config.py` + environment variables. A shared multi-tenant
config store is therefore not required for the POC. The `workspace_config`
(keyed by `org_id`) externalisation described above is only needed once a
single shared multi-tenant deployment is on the table — that work is deferred
to a post-POC story.

## OAuth Connector Secrets

Each connector's client secret is resolved from the environment at runtime via `secret_key`
in `ConnectorAuthConfig`. No secret value is ever stored in code.

Set the following variables in your deployment environment (or `.env` for local dev).
See `backend/.env.example` for the full list including non-secret client IDs.

### Required connector secrets

| Connector     | Environment variable         | Flow                  |
|---------------|------------------------------|-----------------------|
| Salesforce    | `SALESFORCE_CLIENT_SECRET`   | authorization_code    |
| ServiceNow    | `SERVICENOW_CLIENT_SECRET`   | authorization_code    |
| Jira          | `JIRA_CLIENT_SECRET`         | authorization_code    |
| Confluence    | `CONFLUENCE_CLIENT_SECRET`   | authorization_code    |
| GitHub        | `GITHUB_CLIENT_SECRET`       | authorization_code    |
| Slack         | `SLACK_CLIENT_SECRET`        | authorization_code    |
| SAP           | `SAP_CLIENT_SECRET`          | client_credentials    |
| Dynamics 365  | `DYNAMICS365_CLIENT_SECRET`  | client_credentials    |

Set each to `your_secret_here` as a placeholder; replace with the real value from the
provider's OAuth app registration before going to production.

### Other required secrets

| Variable                | Purpose                                         |
|-------------------------|-------------------------------------------------|
| `CREDENTIAL_VAULT_KEY`  | Fernet key for encrypting tokens at rest        |
| `DEV_JWT`               | Bearer token for local dev auth (`dev-token-change-me` by default) |
| `JWT_SECRET`            | HS256 signing secret for user-login JWTs (AUTH-1). **Required in production** — issuance fails closed if unset when `ENVIRONMENT=production`. Generate with `openssl rand -hex 32`. |
| `ENVIRONMENT`           | `production` enforces `JWT_SECRET` and fail-closed invite behaviour; unset for dev/test. |

### Notes

- Jira and Confluence share a single Atlassian OAuth app. Set `ATLASSIAN_CLIENT_ID` and
  use separate `JIRA_CLIENT_SECRET` / `CONFLUENCE_CLIENT_SECRET` values.
- SAP and Dynamics 365 use client_credentials flow — no OAuth redirect URI is needed.
- Slack revocation uses the `auth.revoke` Web API (not RFC 7009). No extra config required.

---

## Login Rate Limiting (AUTH-1)

AUTH-1 adds a `login_attempts` table (created by Alembic migration `0004`) that backs
**lightweight application-layer brute-force protection**: after 5 failed login attempts
for the **same email** within 15 minutes, the 6th attempt returns `429`.

**Scoping — per email, not per IP.** The block is keyed on the email only. The original
AUTH-1 AC7 also throttled per source IP, but that locked out legitimate co-located users
(a whole team behind one office NAT, or several POC testers on `localhost`): one user's
failures would block everyone on the shared IP. IP is still recorded in `login_attempts`
for audit, but it is not used as a blocking key. If you need IP-level brute-force
protection across many emails, enforce it at the gateway (see table below).

**`Retry-After` reflects the actual remaining time.** Rather than a fixed `900`, the
header (and a `retry_after` field in the `429` JSON body) carries the real seconds left
until the block lifts — i.e. when the oldest of the 5 most-recent failures ages out of
the 15-minute window. It is also placed in the body because `Retry-After` is not a
CORS-safelisted response header, so the browser SPA cannot read the header cross-origin;
the body field lets the login form show a live "wait N minutes" countdown.

**Deployment assumption — is the table needed?**

| Deployment posture | login_attempts table |
|---|---|
| No upstream rate limiting | **Required** — it is the only protection. |
| Behind an API gateway / reverse proxy (AWS API Gateway, nginx, Cloudflare) with login rate limiting already configured | **Optional / redundant** — the gateway enforces the limit. |

If you rely on a gateway, document that decision here per the deployment, but do **not**
drop the table unless the gateway protection is confirmed. The table is cheap and
fail-safe; the application-layer check is a defence-in-depth baseline.

---

## AUTH-1 — Tracked Design Debt & Deferred Hardening

These are deliberate POC scoping decisions and design tradeoffs in the AUTH-1
auth layer. They are **not bugs** — they are choices made to ship a working
login surface quickly — but each must be revisited before AUTH-1 is treated as
production-grade. File/keep a linked follow-up ticket for every item before
closing the AUTH-1 epic.

The code-level security bugs from the review (signature-less role/jti decode,
bcrypt byte truncation, password-change session revocation, org self-join by
name, weak-secret warning) have been **fixed** in code. The items below are the
ones the review asked to be *tracked* rather than fixed inline.

### P1 — Targeted account-lockout via email-scoped rate limiting (review #7)

**Where:** `backend/app/auth/user_auth.py` — `check_login_rate_limit()`.

**What:** Rate limiting is scoped to the email only (5 failed attempts / 15 min
→ 429). This was a deliberate deviation from AUTH-1 AC7's per-IP limiting,
because per-IP locked out co-located POC testers sharing one office NAT /
localhost IP (see the module docstring rationale and the "Login Rate Limiting"
section above).

**Risk:** Because the throttle keys on email alone, an attacker who knows a
victim's email can deliberately submit 5 bad passwords and lock that account out
for 15 minutes on demand — a cheap, repeatable denial-of-service against a
specific user. For time-sensitive enterprise workflows (nCino loan origination,
STRS benefit decisions) a targeted lockout has real operational impact.

**Intended fix (post-POC):** Move to a combined `(email, ip)` throttle key, or a
two-tier scheme — a higher per-IP threshold to blunt distributed brute force
plus a per-`(email, ip)` lockout — so a single attacker IP cannot lock an
account globally and a legitimate user on a different IP is never affected.
Alternatively, delegate rate limiting to the API gateway (see review #8 / the
rate-limiting deployment table above) and reduce the application-layer limiter
to defence in depth.

**Owner:** Backend. **Status:** open follow-up.

### P2 — Dual DDL path: `ensure_auth_tables()` vs Alembic (review #8)

**Where:** `backend/app/main.py` startup → `app.auth.user_auth.ensure_auth_tables()`
and migrations `0004_create_users_and_login_attempts.py` / `0005_create_orgs.py`.

**What:** The runtime `ensure_auth_tables()` runs `CREATE TABLE IF NOT EXISTS`
on startup; Alembic migrations create the same tables. This mirrors the existing
`ensure_entities_table()` pattern (flagged in PR #104) and exists because
`seed_loader.py` does not run Alembic, so a fresh dev DB needs the tables created
some other way.

**Risk:** In an environment where the app starts before `alembic upgrade head`
runs, the tables already exist (created by `ensure_auth_tables()`) but are absent
from Alembic's `alembic_version` tracking. A later `alembic upgrade head`
silently succeeds (the DDL is `IF NOT EXISTS`) and stamps the head revision,
skipping any *data*/transform logic those migrations might carry.

**Intended fix (post-POC):** Treat `ensure_auth_tables()` as a development
convenience only. In staging/production, Alembic is the single source of truth —
run `alembic upgrade head` as a deploy step and gate or skip the runtime DDL
there (e.g. via an env flag). The DDL itself can never drift because the
migration and the runtime path both import the same `ALL_*_DDL` tuples.

**Owner:** Backend / DevOps. **Status:** open follow-up.

### P2 — Global email uniqueness blocks multi-org users (review #12)

**Where:** `backend/database/models/users.py` — `idx_users_email_unique`.

**What:** `email` is unique across the entire platform. A consultant who
legitimately needs access to two customer workspaces cannot register twice.
Documented in `users.py` as the canonical POC constraint.

**Intended fix (post-POC):** Build the multi-org identity model — move the unique
constraint from `email` to `(email, org_id)`, add a separate identity anchor to
`users`, and migrate `idx_users_email_unique`. Joining a second workspace is via
invite only (org self-join by name has been removed — review #5).

**Owner:** Backend. **Status:** open follow-up.

### Deployment assumption — rate-limiting layer

If the deployment sits behind an API gateway / reverse proxy (AWS API Gateway,
nginx, Cloudflare) with rate limiting already configured, the `login_attempts`
table is redundant. Confirm the gateway protection and document the decision in
the "Login Rate Limiting (AUTH-1)" section above before removing the table. Do
not delete it on assumption.

---

## Database Driver Inventory

### 1. Microsoft ODBC Driver 18 — SQL Server

| Property | Value |
|---|---|
| Driver package | `msodbcsql18` |
| Install method | Microsoft apt repository (GPG key + signed apt source) |
| Python package | `pyodbc>=5.0.1` |
| System dependency | `unixodbc`, `unixodbc-dev` |
| Dockerfile stage | Stage 2 |

**Why ODBC Driver 18?**

SQL Server connectivity in Python goes through the ODBC stack:
`Python` → `pyodbc` → `unixodbc` → `msodbcsql18` → SQL Server.

Driver 18 is the current Microsoft-supported version. It enforces TLS encryption
by default — no additional configuration is required to achieve encrypted connections
to SQL Server. Earlier drivers (17 and below) require explicit `Encrypt=yes` in the
connection string; Driver 18 flips the default so SQL Server connections are
encrypted out of the box.

**Installation steps (Dockerfile)**

```dockerfile
# 1. Import Microsoft's GPG signing key
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg

# 2. Add Microsoft's Debian 12 (Bookworm) apt source
RUN curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        | tee /etc/apt/sources.list.d/mssql-release.list

# 3. Install the driver — ACCEPT_EULA=Y is mandatory for non-interactive installs
RUN apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18
```

**Known constraints**

- `ACCEPT_EULA=Y` must be set as an environment variable during install. This is
  the mechanism Microsoft documents for non-interactive (CI/Docker) installs — it
  is not a workaround.
- The Microsoft apt repository URL is distribution-specific. The URL above is for
  Debian 12 (Bookworm). If the base image is updated to a newer Debian release,
  update `/config/debian/12/prod.list` → `/config/debian/13/prod.list` (or the
  equivalent Ubuntu path).
- `msodbcsql18` requires `unixodbc` to be installed first. Order matters in the
  Dockerfile.

**Reference:** https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server

---

### 2. Oracle Instant Client 21 — Oracle DB

| Property | Value |
|---|---|
| Driver package | `oracle-instantclient21-basic` (RPM) |
| Install method | Oracle public YUM repository → `alien` conversion → `.deb` install |
| Python package | `oracledb>=2.0.0` |
| System dependency | `libaio1`, `alien`, `wget` |
| Dockerfile stage | Stage 3 |
| Version pinned | 21.3.0.0.0-1 |

**Why Oracle Instant Client 21?**

`oracledb` (Oracle's official Python driver, successor to `cx_Oracle`) requires
Oracle Instant Client 21 at runtime to handle the native Oracle Net protocol.
Version 21 is the Long-Term Support release and is required for Oracle Database
12.1 through 21c compatibility.

**Why alien instead of a native .deb?**

Oracle distributes Instant Client as RPM packages via their public YUM repository
at `yum.oracle.com`. Oracle does not publish a native Debian apt repository for
Instant Client. The standard pattern for enterprise Debian/Ubuntu containers is to
use `alien` to convert the RPM to a `.deb` for installation.

No Oracle account or authentication is required to download the packages from
`yum.oracle.com` — the repository is publicly accessible.

**Installation steps (Dockerfile)**

```dockerfile
# 1. Download the basic RPM from Oracle's public YUM repo (no auth required)
RUN wget -q \
    "https://yum.oracle.com/repo/OracleLinux/OL8/oracle/instantclient/x86_64/getPackage/oracle-instantclient21.3-basic-21.3.0.0.0-1.x86_64.rpm" \
    -O /tmp/oracle-ic-basic.rpm

# 2. Convert RPM to .deb and install with alien
RUN alien --install --scripts /tmp/oracle-ic-basic.rpm \
    && rm -f /tmp/oracle-ic-basic.rpm

# 3. Register the shared library path so the dynamic linker finds libclntsh.so
RUN echo "/usr/lib/oracle/21.3/client64/lib" \
        > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig

# 4. Set environment variables for Oracle runtime
ENV ORACLE_HOME=/usr/lib/oracle/21.3/client64
ENV LD_LIBRARY_PATH=/usr/lib/oracle/21.3/client64/lib
ENV PATH=/usr/lib/oracle/21.3/client64/bin:${PATH}
```

**Known constraints**

- `alien` (with Perl) adds approximately 30 MB to the final image. This is accepted
  for Sprint 10. A future story can evaluate switching to Oracle's Ubuntu `.deb`
  packages if Oracle publishes a supported build for Debian Bookworm.
- The version URL (`21.3.0.0.0-1`) is pinned for build reproducibility. When
  upgrading to 21.4 or later, update the URL and verify the package name.
- `libaio1` is required at runtime for Oracle's async I/O path. On Debian Bookworm,
  `libaio1` is a transitional package that installs `libaio1t64`; both names work.
- Oracle Instant Client 21 is compatible with Oracle Database 12.1 through 23c.
  DBA access is not required — read-only service account credentials are sufficient
  for the AgentIQ use case.
- `ldconfig` must be run after installing the Instant Client so the system dynamic
  linker registers `libclntsh.so`. Without this step, `import oracledb` will fail
  at runtime with a shared library not found error.

**Reference:** https://www.oracle.com/database/technologies/instant-client.html

---

### 2b. Oracle DB — Thin Mode vs Thick Mode (T2-S12-A)

**Default deployment: thin mode (no Instant Client required)**

`oracledb` supports two operating modes:

| Mode | Instant Client required | `init_oracle_client()` called | Use case |
|---|---|---|---|
| **Thin (default)** | No | Never | Standard deployment — Docker, dev, CI |
| **Thick** | Yes (version 21+) | Yes, once at startup | Pre-12.1 Oracle, Kerberos auth, TNS name resolution |

The AgentIQ Oracle ingestor (`backend/connectors/db/oracle_ingestor.py`) uses **thin mode
by default**. `oracledb.init_oracle_client()` is **not called** anywhere in the standard
deployment path. This means:

- No Oracle Instant Client package is required in the standard Docker image.
- The Oracle Instant Client section (2) above documents thick mode for escalation only.
- Thin mode supports Oracle Database 12.2 and later with direct TCP connections.
- Oracle Autonomous Database / Oracle Cloud wallet mTLS connections require
  thick mode plus wallet configuration; set `ORACLE_THICK_MODE=1` and follow
  the thick mode escalation path.

**When to escalate to thick mode**

Thick mode is required only in these specific cases:

1. **Pre-12.1 Oracle databases** — thin mode's wire protocol requires Oracle 12.2+.
2. **Kerberos or external authentication** — thin mode does not support OS-authenticated
   connections or Kerberos tickets; thick mode delegates to the native Oracle stack.
3. **TNS name resolution via `tnsnames.ora`** — thin mode accepts EZConnect and direct
   DSN strings, but does not read `tnsnames.ora` or `sqlnet.ora` from the filesystem.
   If the Oracle DBA requires TNS aliases, thick mode is needed.

**Activating thick mode (escalation path only)**

Call `init_thick_mode()` from `backend/connectors/db/oracle.py` once at container
startup, before any connection is made. This is the only supported escalation path.

```python
# In your container entrypoint or ASGI lifespan hook — NOT in the ingestor:
from backend.connectors.db.oracle import init_thick_mode
init_thick_mode()  # optionally pass lib_dir='/usr/lib/oracle/21.3/client64/lib'
```

The Oracle Instant Client Dockerfile steps in section 2 above are the prerequisite for
thick mode. Do not add those steps to the standard image unless thick mode is required.

**Thick mode is never activated automatically.** The ingestor will not call
`init_oracle_client()` under any circumstances. Escalation requires an explicit
engineering decision and a Dockerfile change.

---

### 3. psycopg2-binary — PostgreSQL

| Property | Value |
|---|---|
| Driver package | `psycopg2-binary` |
| Install method | `pip install` via `requirements.txt` |
| Python package | `psycopg2-binary>=2.9.9` |
| System dependency | None — `libpq` is bundled in the binary wheel |
| Dockerfile stage | Stage 4 (pip install) |

**Why psycopg2-binary?**

`psycopg2-binary` is the simplest driver for PostgreSQL connectivity. Unlike the
source (`psycopg2`) package, the `-binary` variant ships with `libpq` pre-compiled
and bundled inside the wheel. No system-level PostgreSQL client libraries need to
be installed, and the build does not require a C compiler.

This is appropriate for the Sprint 10 bootstrap. If performance profiling in a later
sprint identifies the bundled `libpq` as a bottleneck (unlikely for the AgentIQ
read-only workload), the project can switch to the source package with a
system-installed `libpq`.

**Installation (requirements.txt)**

```
psycopg2-binary>=2.9.9
```

No Dockerfile changes beyond `pip install -r requirements.txt` are needed.

**Known constraints**

- `psycopg2-binary` is not recommended for production packages that are distributed
  as libraries (due to bundled `libpq` version conflicts with other packages). For
  an application container such as this one, it is the correct choice.
- SSL mode is configured at connection time via `sslmode` in the connection string.
  The bundled `libpq` respects standard PostgreSQL SSL environment variables
  (`PGSSLMODE`, `PGSSLROOTCERT`, etc.) if needed.

---

## Image Size and Build Time Tradeoff

Track 2 increases the Docker image size compared to previous sprints. This is an
**informed engineering tradeoff, not a design flaw**.

| Component | Approximate size addition |
|---|---|
| Base image (`python:3.11-slim-bookworm`) | ~150 MB |
| Build tools (`curl`, `gnupg`, `alien`, `wget`, etc.) | ~60 MB |
| Microsoft ODBC Driver 18 + unixodbc | ~25 MB |
| Oracle Instant Client 21 (basic) | ~80 MB |
| psycopg2-binary | ~5 MB |
| Python app + dependencies | ~60 MB |
| **Estimated total** | **~380 MB** |

Leadership has acknowledged that Track 2 is not a standard connector track.
Native libraries for enterprise databases (SQL Server, Oracle, PostgreSQL) are
unavoidable requirements — they cannot be shimmed in Python alone.

Future optimisation opportunities (not in Sprint 10 scope):
- Multi-stage builds to separate build tools from the runtime image
- Switching from `python:3.11-slim` to a custom base that pre-installs Oracle IC
- Oracle Instant Client "Basic Lite" package (smaller, fewer NLS charsets) if
  multi-byte character encoding is not required

---

## Environment Variables Required at Runtime

The following environment variables must be present when the container starts.
They are passed via `--env-file backend/.env` in development and injected by the
secrets manager in production. **Never bake credentials into the image.**

| Variable | Used by | Description |
|---|---|---|
| `SQLSERVER_USERNAME` | SQL Server connector | Env-var key resolved by `resolve_secret()` |
| `SQLSERVER_PASSWORD` | SQL Server connector | Env-var key resolved by `resolve_secret()` |
| `ORACLE_HOST` | Oracle DB connector | Oracle host name used by the runner |
| `ORACLE_PORT` | Oracle DB connector | Oracle listener port, default `1521` |
| `ORACLE_DATABASE` | Oracle DB connector | Oracle service/database name, default `ORCL` |
| `ORACLE_DB_USERNAME` | Oracle DB connector | Env-var key resolved by `resolve_secret()` |
| `ORACLE_DB_PASSWORD` | Oracle DB connector | Env-var key resolved by `resolve_secret()` |
| `POSTGRESQL_HOST` | PostgreSQL connector | PostgreSQL host name used by the runner |
| `POSTGRESQL_PORT` | PostgreSQL connector | PostgreSQL port, default `5432` |
| `POSTGRESQL_DATABASE` | PostgreSQL connector | PostgreSQL database name, default `postgres` |
| `POSTGRESQL_USERNAME` | PostgreSQL connector | Env-var key resolved by `resolve_secret()` |
| `POSTGRESQL_PASSWORD` | PostgreSQL connector | Env-var key resolved by `resolve_secret()` |
| `DEV_JWT` | Auth middleware | Bearer token for development |
| `DB_PATH` | SQLite store | Path to the application database |

> **Note:** The `*_USERNAME` / `*_PASSWORD` naming is the Sprint 10 bootstrap
> credential model. T1 owns the credential vault migration story; ingestors
> should not depend on these variable names being permanent.

---

## Updating Driver Versions

| Driver | Where to update | Verification |
|---|---|---|
| ODBC Driver 18 | `mssql-release.list` apt source pinned via distro config URL | Run `odbcinst -q -d -n "ODBC Driver 18 for SQL Server"` inside container |
| Oracle IC 21 | RPM URL in Dockerfile Stage 3 | Run `ls /usr/lib/oracle/` inside container |
| psycopg2-binary | `requirements.txt` version constraint | `python -c "import psycopg2; print(psycopg2.__version__)"` |

---

---

## Oracle DB Driver Mode — Thin vs Thick (T2-S12-A)

The `oracledb` Python package supports two operating modes.

### Thin mode (default — no Instant Client required)

Thin mode is the default for all standard AgentIQ deployments.
`oracledb.init_oracle_client()` is **not called** in thin mode.
The `oracle_ingestor.py` module never calls `init_oracle_client()`.

| Property | Thin mode |
|---|---|
| Oracle Instant Client required? | **No** |
| Oracle versions supported | 12.1 and later |
| `init_oracle_client()` called? | No |
| Docker image impact | `pip install oracledb` only |

### Thick mode — escalation path only

Escalate to thick mode only when the customer reports one of:
- `ORA-12560: TNS: protocol adapter error`
- Oracle version earlier than 12.1
- Kerberos / external authentication required
- TNS name resolution via `tnsnames.ora`

**How to configure `oracle_thick` mode:**

1. Install Oracle Instant Client 21 in the container (see Stage 3 above).

2. Call `init_oracle_client()` once at container startup (before any connection):
   ```python
   from backend.connectors.db.oracle import init_thick_mode
   init_thick_mode(lib_dir="/usr/lib/oracle/21.3/client64/lib")
   ```

3. Set `DBConnectorConfig.driver = 'oracle_thick'` for the connector:
   ```python
   config = DBConnectorConfig(connector_id="oracle_db", driver="oracle_thick", ...)
   ```

| Property | Thick mode |
|---|---|
| Oracle Instant Client required? | Yes — version 21 |
| Oracle versions supported | All, including pre-12.1 |
| `init_oracle_client()` called? | Yes — once at startup via `init_thick_mode()` |
| Docker image impact | +~80 MB (Instant Client) |

---

*Document maintained by Track 2 — Enterprise Technology. Contact the T2-S10-A
story owner before modifying Dockerfile driver installation stages.*
