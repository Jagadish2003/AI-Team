# AgentIQ 2.0 — Deployment Guide

**T2-S10-A | Database Connectivity Framework | Sprint 10**

---

## Overview

Track 2 (Enterprise Technology) introduces native database drivers for SQL Server,
Oracle DB, and PostgreSQL. These require system-level packages installed at image
build time, in addition to the Python-level packages in `requirements.txt`.

This document records every driver addition, the installation rationale, known
build-time constraints, and the engineering tradeoffs acknowledged by leadership.

---

## Quick Start

```bash
# Build the image (from the backend/ directory)
docker build -t agentiq-backend ./backend

# Run with environment variables
docker run --env-file backend/.env -p 8000:8000 agentiq-backend
```

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
| `ORACLE_DB_USERNAME` | Oracle DB connector | Env-var key resolved by `resolve_secret()` |
| `ORACLE_DB_PASSWORD` | Oracle DB connector | Env-var key resolved by `resolve_secret()` |
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
