# AgentIQ Deployment — Environment Variables

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

### Notes

- Jira and Confluence share a single Atlassian OAuth app. Set `ATLASSIAN_CLIENT_ID` and
  use separate `JIRA_CLIENT_SECRET` / `CONFLUENCE_CLIENT_SECRET` values.
- SAP and Dynamics 365 use client_credentials flow — no OAuth redirect URI is needed.
- Slack revocation uses the `auth.revoke` Web API (not RFC 7009). No extra config required.
