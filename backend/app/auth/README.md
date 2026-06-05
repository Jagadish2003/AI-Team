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
| dynamics365 | `DYNAMICS365_CLIENT_SECRET`       |

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

## Two-Phase OAuth Callback Pattern

Real OAuth providers redirect to a registered callback URI after the user authorises. The callback
receives the code, exchanges it for a token, then redirects the user to the Integration Hub frontend.
Understanding both phases is essential for correct `OAUTH_REDIRECT_URI` configuration.

### Phase 1 — Provider → AgentIQ callback endpoint

The OAuth provider redirects to `OAUTH_REDIRECT_URI` after the user grants access.

- `OAUTH_REDIRECT_URI` **must match the URI registered with the provider exactly** — character for
  character, including scheme, host, port, and path.
- A mismatch causes the provider to return a `redirect_uri_mismatch` error and reject the flow.
- Example: `OAUTH_REDIRECT_URI = 'https://agentiq.app/api/connectors/oauth/callback'`

### Phase 2 — AgentIQ callback → Integration Hub frontend

After the code exchange succeeds and the token is stored, the callback issues an internal redirect
to the Integration Hub frontend. This redirect goes to `OAUTH_SUCCESS_REDIRECT` — it is **not**
sent to the OAuth provider.

- `OAUTH_SUCCESS_REDIRECT = '/integration-hub?connected={connector_id}'`
- This is an internal frontend route, not a provider URI.

### Reverse proxy note

When AgentIQ runs behind a reverse proxy (e.g. nginx, AWS ALB), the proxy forwards public traffic
to the internal app host. `OAUTH_REDIRECT_URI` must be the **public-facing URI** — the one
registered with the provider — not the internal host.

If the proxy rewrites paths, ensure the callback path is preserved end-to-end.

```
# Example: proxy forwards https://agentiq.app/api/* → http://app:8000/api/*

# Correct — matches the URI registered with the provider:
OAUTH_REDIRECT_URI = 'https://agentiq.app/api/connectors/oauth/callback'

# Wrong — internal host is not registered with the provider and will cause redirect_uri_mismatch:
OAUTH_REDIRECT_URI = 'http://app:8000/api/connectors/oauth/callback'
```
