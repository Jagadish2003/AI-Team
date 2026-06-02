# backend/app/auth — OAuth Auth Framework & Credential Vault

## secret_key Naming Convention

`ConnectorAuthConfig.secret_key` holds the **environment variable name** that contains the
client secret — never the secret value itself. This allows the config to be stored safely
without exposing credentials.

Pattern: `{CONNECTOR_ID_UPPER}_CLIENT_SECRET`

| Connector   | Expected `secret_key` value       |
|-------------|-----------------------------------|
| salesforce  | `SALESFORCE_CLIENT_SECRET`        |
| servicenow  | `SERVICENOW_CLIENT_SECRET`        |
| jira        | `JIRA_CLIENT_SECRET`              |
| github      | `GITHUB_CLIENT_SECRET`            |
| confluence  | `CONFLUENCE_CLIENT_SECRET`        |
| slack       | `SLACK_CLIENT_SECRET`             |
| sap         | `SAP_CLIENT_SECRET`               |
| dynamic365  | `DYNAMIC365_CLIENT_SECRET`        |

At runtime, callers resolve the secret via `os.environ[config.secret_key]` (implemented in T2/secrets.py).

## Interface Lock Rule

**The connector interface is locked after AT-73 merges.**

- You MAY add new optional fields (with a default value).
- You MUST NOT rename or remove any existing field.
- You MUST NOT add a `client_secret` field — store only the env var name in `secret_key`.

Any change that renames or removes a field is a breaking change and requires a new model version.

## File Map

| File          | Contents                                          | Ticket |
|---------------|---------------------------------------------------|--------|
| `models.py`   | `ConnectorAuthConfig`, `TokenRecord`, `ConnectorNotAuthenticatedError` | T1 (AT-73) |
| `secrets.py`  | `resolve_secret(secret_key)` — reads env var, fails fast if missing | T2 |
| `oauth.py`    | `authorization_code_flow()`, `client_credentials_flow()`, token refresh | T3/T4 |
| `vault.py`    | `get_token()`, `store_token()`, `revoke_token()` — Fernet encryption at rest | T5/T6 |
| `configs.py`  | Per-connector `ConnectorAuthConfig` instances for all 8 connectors | T9 |

## Flows

- `authorization_code`: used by Salesforce, ServiceNow, Jira, Confluence, GitHub (has revocation endpoint)
- `client_credentials`: used by SAP, Dynamic365; no `refresh_token`, no `redirect_uri`
