# Connector Health Check Sample Payloads — AT-93

Sample JSON payloads for all 8 connectors covering every valid health state.
These fixtures feed directly into `test_telemetry.py` contract tests once approved (AT-92/AT-93).

## Telemetry Schema (telemetry_events table — T1-S10-C)

| Column | Type | Description |
|---|---|---|
| `id` | TEXT (UUID) | Auto-generated per event |
| `org_id` | TEXT | Workspace identifier |
| `event_type` | TEXT | Always `connector.health_check` for these fixtures |
| `source` | TEXT | Always `connector_health_job` — set in `connector_health.py:143` |
| `connector_id` | TEXT | Which connector was checked |
| `duration_ms` | INTEGER | Health check round-trip time in milliseconds |
| `success` | INTEGER | Always `1` (true) — per-connector failures are caught and logged, not reflected here |
| `payload` | TEXT (JSON) | `status`, `connector_id`, `token_expiry_seconds`, `check_duration_ms` |
| `timestamp` | TEXT (ISO-8601 UTC) | Event time |

> Event-specific fields live inside the `payload` JSON column per `ConnectorHealthPayload` TypedDict in `backend/app/telemetry.py`.

---

## Status Values

Produced by `_map_status()` in `backend/app/jobs/connector_health.py` — priority order: `needs_refresh` → `connected` → `needs_auth`.

| Status | Meaning | UI dot colour |
|---|---|---|
| `connected` | Token valid, no action needed | Green |
| `needs_refresh` | Token within `REFRESH_THRESHOLD_SECONDS` (default 300s) — silent refresh pending | Green |
| `needs_auth` | Token expired, revoked, or credentials invalid — user action required | Amber |

> `needs_refresh` is an **internal operational state** only. The Integration Hub UI maps both `connected` and `needs_refresh` to the green dot (T1-S10-A spec).

---

## Token Expiry Reference

| Connector | Auth flow | `token_expiry_seconds` (connected) | `needs_refresh` valid? | Source |
|---|---|---|---|---|
| Salesforce | authorization_code | 7200 | ✅ Yes | AT-93 spec |
| ServiceNow | authorization_code | 1800 | ✅ Yes | AT-93 spec |
| Jira | authorization_code | 3600 | ✅ Yes | Atlassian standard 1h TTL |
| D365 | client_credentials | 3600 | ✅ Yes | Azure AD standard 1h TTL |
| SAP | client_credentials | 1800 | ❌ No — 2 states only | No refresh token in client_credentials flow |
| GitHub | authorization_code | null | ❌ No — 2 states only | PATs are non-expiring by default |
| Confluence | authorization_code | 3600 | ✅ Yes | Same Atlassian token endpoint as Jira |
| Slack | authorization_code | null | ❌ No — 2 states only | Bot tokens non-expiring; revocation → needs_auth |

> `needs_refresh` `token_expiry_seconds` value is **45** across all connectors — within the 300s `REFRESH_THRESHOLD_SECONDS` default (`vault.py:27`).
> `needs_auth` `token_expiry_seconds` is always **null** — no valid token present.

---

## Files in This Directory

| File | Connector | Scenarios | connector_id used |
|---|---|---|---|
| `connector_health_salesforce.json` | Salesforce | connected, needs_refresh, needs_auth | `salesforce_prod` |
| `connector_health_servicenow.json` | ServiceNow | connected, needs_refresh, needs_auth | `servicenow_prod` |
| `connector_health_jira.json` | Jira | connected, needs_refresh, needs_auth | `jira_cloud` |
| `connector_health_d365.json` | D365 | connected, needs_refresh, needs_auth | `d365_finance` |
| `connector_health_sap.json` | SAP | connected, needs_auth | `sap_s4hana` |
| `connector_health_github.json` | GitHub | connected, needs_auth | `github_cloud` |
| `connector_health_confluence.json` | Confluence | connected, needs_refresh, needs_auth | `confluence_cloud` |
| `connector_health_slack.json` | Slack | connected, needs_auth | `slack_workspace` |

Once approved, these fixtures are referenced by `backend/tests/contract/test_telemetry.py`.

---

## Design Notes

- **`success` is always `true`** in health check events. A connector check failure (e.g. network error) is caught per-connector inside `_check_connector()` and logged at ERROR — it does not write a telemetry event with `success=false`. Only successful health evaluations produce a `connector.health_check` event.
- **SAP `sap_note`** on the `needs_auth` scenario: `"SAP client_credentials session expired — no refresh token, re-auth required"`.
- **`connector_id` naming convention** matches existing audit_samples fixtures: `<connector>_<instance_type>` (e.g. `salesforce_prod`, `jira_cloud`).
