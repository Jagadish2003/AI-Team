# ──────────────────────────────────────────────────────────────────────────────
# AgentIQ 2.0  |  Backend  |  T2-S10-A  |  Database Connectivity Framework
# ──────────────────────────────────────────────────────────────────────────────
#
# Track 2 adds three native database drivers to this image:
#
#   Driver              Database     Install method
#   ──────────────────  ───────────  ──────────────────────────────────────────
#   ODBC Driver 18      SQL Server   Microsoft apt repository (GPG + apt source)
#   Oracle Instant      Oracle DB    Oracle public YUM repo RPM → alien → .deb
#     Client 21
#   psycopg2-binary     PostgreSQL   pip (libpq bundled — no system lib needed)
#
# This increases image size and build time relative to previous sprints.
# That is an informed engineering tradeoff acknowledged by leadership — see
# T2-S10-A deployment note in the story and deployment/README.md.
#
# Build:  docker build -t agentiq-backend .
# Run:    docker run --env-file .env -p 8000:8000 agentiq-backend
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim-bookworm

# Suppress interactive prompts from apt and Oracle/Microsoft package scripts
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# ── Stage 1: shared build tools ───────────────────────────────────────────────
#
# curl + gnupg + apt-transport-https  — needed to add third-party apt repos
# unixodbc + unixodbc-dev             — ODBC runtime; required by pyodbc
# libaio1                             — async I/O lib required by Oracle IC
# alien                               — converts Oracle's RPM packages to .deb
# wget                                — downloads Oracle RPM from public repo
#
# All packages pinned to no explicit version so apt resolves the latest
# compatible set for Debian Bookworm; individual driver stages are version-
# pinned where reproducibility is critical (ODBC 18, Oracle IC 21.3).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        apt-transport-https \
        ca-certificates \
        unixodbc \
        unixodbc-dev \
        libaio1 \
        alien \
        wget \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: Microsoft ODBC Driver 18 (SQL Server) ────────────────────────────
#
# Steps (per Microsoft official documentation for Debian 12 / Bookworm):
#   1. Import Microsoft's GPG signing key into the apt keyring
#   2. Add Microsoft's Debian 12 package list as a signed apt source
#   3. Install msodbcsql18 with ACCEPT_EULA=Y (required by the Microsoft EULA
#      for non-interactive installs — this is the documented mechanism, not a
#      workaround)
#
# msodbcsql18 depends on unixodbc (installed in Stage 1).
# SSL is enforced by default in Driver 18; no extra config needed.
#
# Reference:
#   https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/
#   installing-the-microsoft-odbc-driver-for-sql-server
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        | tee /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 3: Oracle Instant Client 21 (Oracle DB) ─────────────────────────────
#
# Oracle distributes Instant Client as RPM packages via their public YUM
# repository at yum.oracle.com — no Oracle account or authentication required
# for the packages themselves.
#
# On Debian Bookworm (apt-based), the standard enterprise pattern is:
#   1. Download the RPM from Oracle's public repo with wget
#   2. Convert RPM → .deb with alien and install in one step
#   3. Register the shared library path with ldconfig so oracledb/cx_Oracle
#      can find libclntsh.so at runtime
#
# Version 21.3.0.0.0-1 is pinned for reproducibility; update the URL and
# confirm the SHA-256 when upgrading.
#
# Known build constraint: alien adds ~30 MB of overhead to the final image
# (Perl + rpmlib). This is accepted for now; a future story can switch to a
# slim Oracle-provided Debian package if Oracle publishes one for Bookworm.
#
# Reference:
#   https://www.oracle.com/database/technologies/instant-client.html
#   https://yum.oracle.com/oracle-instant-client.html
RUN wget -q \
        "https://yum.oracle.com/repo/OracleLinux/OL8/oracle/instantclient/x86_64/getPackage/oracle-instantclient21.3-basic-21.3.0.0.0-1.x86_64.rpm" \
        -O /tmp/oracle-ic-basic.rpm \
    && alien --install --scripts /tmp/oracle-ic-basic.rpm \
    && rm -f /tmp/oracle-ic-basic.rpm \
    && echo "/usr/lib/oracle/21.3/client64/lib" \
        > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*

# Make Oracle libraries and binaries visible to the process and to Python
ENV ORACLE_HOME=/usr/lib/oracle/21.3/client64
ENV LD_LIBRARY_PATH=/usr/lib/oracle/21.3/client64/lib
ENV PATH=/usr/lib/oracle/21.3/client64/bin:${PATH}

# ── Stage 4: Python dependencies (includes psycopg2-binary) ───────────────────
#
# requirements.txt lists all Python dependencies including the three DB driver
# Python packages:
#   pyodbc            — Python ODBC bridge for SQL Server (needs msodbcsql18)
#   oracledb          — Pure-Python Oracle driver (needs Instant Client 21)
#   psycopg2-binary   — PostgreSQL driver (ships libpq bundled; no system lib)
#
# --no-cache-dir keeps the layer small by not storing the pip HTTP cache.
# Requirements are copied before app source so Docker caches this layer and
# only rebuilds it when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 5: Application source ───────────────────────────────────────────────
COPY . .

# Set Python path so `import app` resolves correctly (matches run.sh behaviour)
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
