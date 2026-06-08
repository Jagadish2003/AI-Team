# AgentIQ — Oracle DB Connector Deployment Guide

## Oracle Driver Strategy

AgentIQ uses the `oracledb` Python package for Oracle DB connectivity.
**Thin mode is the default.** No Oracle Instant Client is required in the standard deployment.

---

## Thin Mode (Default — All Standard Deployments)

| Property | Value |
|---|---|
| Default? | Yes |
| Oracle Instant Client required? | No |
| Supported Oracle versions | Oracle 12.1 and later |
| Docker image impact | `pip install oracledb` only — image stays small |
| `init_oracle_client()` called? | **No** |

Thin mode is activated automatically when `oracledb.init_oracle_client()` is **not** called.
The `oracle_ingestor.py` module never calls `init_oracle_client()`.

Oracle Autonomous Database and Oracle Cloud wallet/mTLS connections require
thick mode plus wallet configuration. Set `ORACLE_THICK_MODE=1` and follow the
thick mode escalation path below when a customer provides an Oracle wallet.

### Standard Docker image — no changes needed

```dockerfile
# Standard — no Oracle Instant Client needed
RUN pip install oracledb
```

---

## Thick Mode — Escalation Path Only

Thick mode requires Oracle Instant Client 21 installed in the container.
**Do not enable thick mode by default.**

### When to escalate to thick mode

Escalate to thick mode only when the customer reports one of the following:

| Symptom | Cause |
|---|---|
| `ORA-12560: TNS: protocol adapter error` | Pre-12.1 Oracle or TNS name resolution via `tnsnames.ora` |
| Oracle version earlier than 12.1 | Thick mode required for full compatibility |
| Kerberos or external authentication | Thick mode required for advanced auth |
| TNS name resolution via `tnsnames.ora` | Thin mode does not support `tnsnames.ora` |

### How to configure thick mode

**Step 1** — Install Oracle Instant Client 21 in the container:

```dockerfile
# Thick mode — install Oracle Instant Client 21
RUN apt-get install -y libaio1 && \
    curl -Lo /tmp/instantclient.zip \
      https://download.oracle.com/otn_software/linux/instantclient/214000/instantclient-basiclite-linux.x64-21.4.0.0.0dbru.zip && \
    unzip /tmp/instantclient.zip -d /opt/oracle && \
    rm /tmp/instantclient.zip
ENV LD_LIBRARY_PATH=/opt/oracle/instantclient_21_4:$LD_LIBRARY_PATH
```

**Step 2** — Call `init_thick_mode()` once at container startup (before any connection):

```python
# In container entrypoint or FastAPI lifespan — call once before first connection
from backend.connectors.db.oracle import init_thick_mode
init_thick_mode(lib_dir="/opt/oracle/instantclient_21_4")
```

**Step 3** — Set `DBConnectorConfig.driver = 'oracle_thick'` for the connector:

```python
config = DBConnectorConfig(
    connector_id="oracle_db",
    driver="oracle_thick",   # signals thick mode to the pool factory
    ...
)
```

### Thick mode summary

| Property | Value |
|---|---|
| Default? | No — escalation only |
| Oracle Instant Client required? | Yes — version 21 in container |
| Supported Oracle versions | All versions including pre-12.1 |
| `init_oracle_client()` called? | Yes — called once at startup via `init_thick_mode()` |
| Docker image impact | Significant — Instant Client adds ~100 MB |

---

## Connection Format

Oracle supports two connection formats in thin mode:

| Format | When to use | Example |
|---|---|---|
| EZConnect (direct) | Standard — host:port/service_name | `oracle-host.corp.com:1521/ORCL` |
| TNS descriptor | Thick mode only — complex network topologies | `(DESCRIPTION=(ADDRESS=...))` |

TNS name resolution via `tnsnames.ora` requires thick mode.

---

## Schema Discovery

The Oracle ingestor uses `ALL_COLUMNS` for schema discovery.
If the database user does not have `ALL_COLUMNS` access, it falls back to `USER_COLUMNS`
(current schema only) and logs a warning. Runs continue — no error is raised.

Oracle stores schema and table names in **uppercase** by default.
Scope declarations in AgentIQ must match Oracle's stored case exactly.

---

## Troubleshooting

| Error | Action |
|---|---|
| `ORA-12560: TNS: protocol adapter error` | Escalate to thick mode (see above) |
| `ORA-00942: table or view does not exist` | Grant `SELECT ON ALL_COLUMNS` or `SELECT ON USER_COLUMNS` to the database user |
| `DPI-1047: Cannot locate a 64-bit Oracle Client library` | `init_oracle_client()` was called but Instant Client is not installed — either install Instant Client or switch to thin mode |
| Connection timeout | Check `connect_timeout_s` in `DBConnectorConfig` and firewall rules on port 1521 |
