# Audit Event Sample Payloads — AT-87 SME Review

Sample JSON payloads for SME sign-off on `connector_queried` and `scope_declared` audit events.
These fixtures feed directly into `test_audit.py` contract tests once approved.

## DB Schema (audit_log table — T1-S10-B)

| Column | Type | Description |
|---|---|---|
| `id` | TEXT (UUID) | Auto-generated per event |
| `org_id` | TEXT | Workspace identifier |
| `event_type` | TEXT | Event category string |
| `user_id` | TEXT (nullable) | Acting user — null for system events |
| `run_id` | TEXT (nullable) | Discovery run — null for non-run events |
| `connector_id` | TEXT (nullable) | Source connector |
| `payload` | TEXT (JSON) | Event-specific fields (see below) |
| `timestamp` | TEXT (ISO-8601 UTC) | Event time |

> Event-specific fields (`query_hash`, `row_count`, `scope_type`, etc.) live inside the `payload` JSON column.
> The fixtures below show the full flattened view for readability.

---

## SME1 — connector_queried Review

**Files:** `connector_queried_salesforce.json`, `connector_queried_servicenow.json`, `connector_queried_jira.json`

**Question for SME1:** Do `query_hash` + `row_count` + `duration_ms` satisfy compliance logging requirements for Salesforce SOQL, ServiceNow REST, and Jira JQL queries?

**Critical design decision to confirm:** `query_hash` stores a **SHA-256 hash** of the raw query — the raw query string is **never stored** in the audit log. This protects customer data (queries may contain field values) while still allowing audit verification. Please confirm this approach meets your compliance requirements.

| Field | Value | Notes |
|---|---|---|
| `query_hash` | `sha256:<64-char hex>` | Hash of raw query — raw query never stored |
| `row_count` | integer | Number of rows returned by the query |
| `duration_ms` | integer | Query round-trip time in milliseconds |

---

## SME2 — scope_declared Review

**Files:** `scope_declared_d365.json`, `scope_declared_sap.json`

**Question for SME2:** Do `scope_type` and `scope_values` correctly represent the D365 entity filter and SAP authorization object permission models?

| Connector | `scope_type` | `scope_values` example | Meaning |
|---|---|---|---|
| D365 | `entity_filter` | `["Accounts","Opportunities","Leads"]` | Connector restricted to these Dynamics 365 entity types only |
| SAP | `authorization_object` | `["MM01","FI_GL_ACC","SD_SALES_ORG"]` | SAP ABAP authorization object codes the connector may use |

---

## Files in this directory

| File | Event type | Connector | Reviewer |
|---|---|---|---|
| `connector_queried_salesforce.json` | `connector_queried` | Salesforce | SME1 |
| `connector_queried_servicenow.json` | `connector_queried` | ServiceNow | SME1 |
| `connector_queried_jira.json` | `connector_queried` | Jira | SME1 |
| `scope_declared_d365.json` | `scope_declared` | D365 | SME2 |
| `scope_declared_sap.json` | `scope_declared` | SAP | SME2 |

Once SMEs sign off, these fixtures are referenced by `backend/tests/contract/test_audit.py`.
