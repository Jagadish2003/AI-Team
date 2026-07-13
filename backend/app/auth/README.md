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
| teams       | `TEAMS_CLIENT_SECRET`             |

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
| `models.py`   | `ConnectorAuthConfig`, `TokenRecord`, `StaticCredentialRecord`, `ConnectorNotAuthenticatedError` | T1 (AT-73) |
| `secrets.py`  | `resolve_secret(secret_key)` — reads env var, fails fast if missing | T2 |
| `oauth.py`    | `authorization_code_flow()`, `client_credentials_flow()`, token refresh | T3/T4 |
| `vault.py`    | `get_token()`, `store_token()`, `revoke_token()`, `store/get/revoke_static_credential()` — Fernet encryption at rest | T5/T6 |
| `configs.py`  | Per-connector `ConnectorAuthConfig` instances for all connectors | T9 |

## Vault Record Types (R17-D3 Addendum A, T10)

The `credentials` table holds TWO record types, discriminated by the `kind` column and
sharing the same per-`(org_id, connector_id)` keying and Fernet encryption under
`CREDENTIAL_VAULT_KEY`:

- **`kind='oauth'`** — token records (`TokenRecord`): access/refresh tokens from the OAuth
  flows, auto-refreshed by `get_token()` and the background token-refresher job.
- **`kind='static'`** — static credentials (`StaticCredentialRecord`): entered once by an
  admin, no OAuth dance, no refresh, no expiry. Used by Jira (URL + user + API token),
  ServiceNow (URL + user + password — the OAuth variant uses the existing flow), and the
  native DB connectors (SQL Server / Oracle / PostgreSQL connection credentials).
  `username`/`secret` are Fernet-encrypted at rest (`enc_username`/`enc_secret`); `base_url`
  is the non-secret instance location. API: `store_static_credential()`,
  `get_static_credential()`, `revoke_static_credential()`.

The shared `UNIQUE(org_id, connector_id)` constraint means an org holds ONE credential per
connector across both kinds: storing a static credential replaces an OAuth token for that
connector (and vice versa), which is the intended either/or for ServiceNow. Static rows are
invisible to `get_token()` (it raises `ConnectorNotAuthenticatedError`), and OAuth rows are
invisible to `get_static_credential()` (it returns `None`).

Values are write-only (AC10): an admin can replace a credential but never read one back —
`StaticCredentialRecord` masks `username`/`secret` in `repr` so they cannot leak into logs.

## Auth Modes (R18-A3 T1 / AT-554)

Connector authentication has a **mode** concept broader than any single OAuth grant.
Four modes are recognised (`AuthMode` in `models.py`):

| Mode | Meaning | Inbound callback? |
|------|---------|-------------------|
| `authorization_code` | User-delegated OAuth (browser redirect + callback) | **Yes** |
| `client_credentials` | Service-to-service OAuth | No (outbound-only) |
| `jwt_bearer` | Signed-assertion OAuth (cert in vault) | No (outbound-only) |
| `static` | Vault-stored static credential (API token / user+password / DB creds) | No (outbound-only) |

`app/auth/auth_modes.py` owns the abstraction:

- **Each connector registers its supported modes.** OAuth connectors declare them on
  `ConnectorAuthConfig.supported_auth_modes` (most-preferred first; the first entry is
  the default). Static-only connectors (native DB) are declared in
  `_STATIC_ONLY_SUPPORTED_MODES`. `get_supported_auth_modes()` / `get_default_auth_mode()`
  / `connector_supports_mode()` read this single source of truth. A connector's set
  reflects what actually has a flow behind it today — a mode is added only when its flow
  is built (AT-555 `jwt_bearer`, AT-556/AT-557 `client_credentials`).
- **A per-org configuration selects one.** `set_auth_mode(org_id, connector_id, mode)`
  validates against the supported set and persists the choice on the org's connector
  record; `resolve_auth_mode(org_id, connector_id)` reads it back, falling back to the
  connector default when nothing valid is selected.
- **Every mode terminates in the same vault record shape (AC3).** Downstream ingestion is
  mode-agnostic: it resolves credentials through the unchanged
  `get_connector_credentials(org_id, connector_id)` (`app/auth/credentials.py`) and never
  branches on auth mode. The mode concept lives entirely at the connect/setup edge.
- `OUTBOUND_ONLY_MODES` lists the modes that complete with no inbound HTTPS callback — the
  ones usable in a no-public-inbound deployment (the `NETWORK_PROFILE` UI that consumes
  this is a separate follow-up task).

### Salesforce JWT bearer flow (R18-A3 T2 / AT-555)

`jwt_bearer` is Salesforce's outbound-only headless path (RFC 7523): a signed assertion is
exchanged for an access token — **no client secret, no redirect URI, no inbound callback**.

- **Signing material** (the durable secret) is the connected-app cert **private key**, plus
  the Salesforce username the assertion runs as (`sub`) and the login host (`aud`). It is
  vaulted as a **static** record under the reserved id `{connector_id}:jwt`
  (`store_jwt_bearer_credential` / `get_jwt_bearer_credential` / `revoke_jwt_bearer_credential`),
  so it gets the full credential hygiene — Fernet-encrypted at rest, masked in `repr`,
  write-only through the entry route, never logged (AC5). The consumer key (`iss`) is the
  connected app's non-secret `client_id` from `configs.py`.
- **The minted access token** is cached as the connector's normal OAuth `TokenRecord` (the
  `{connector_id}` row), so downstream resolution via `get_connector_credentials()` is
  identical to any other mode (AC3) and the private key is never resolved as a credential.
- **`get_token()` mints and re-mints.** On first use (no cached token) it signs an assertion
  and exchanges it outbound; on expiry — a JWT bearer token has no OAuth `refresh_token` — it
  re-asserts rather than failing. This is "refresh handled by re-assertion." `instance_url`
  from the response is captured for live-ingest URL resolution, exactly as authorization_code
  does.
- **Entry surface** (owner-only, write-only): `POST/GET/DELETE
  /api/connectors/{connector_id}/jwt-credentials` (`login_url`, `username`, `private_key`).
  Building blocks: `oauth.build_jwt_bearer_assertion()` and `oauth.get_jwt_bearer_token()`.

### Microsoft Graph client-credentials (R18-A3 T3 / AT-556)

`client_credentials` is the Microsoft Graph (Teams / SharePoint) outbound-only path: the app
registration's own `client_id` + `client_secret` are exchanged for a service-identity access
token — **no browser redirect, no inbound callback** (AC2). It is the no-public-inbound
alternative to the delegated authorization_code flow; an org selects it per connector.

- **Credential** is the deployment's app secret (`TEAMS_CLIENT_SECRET` /
  `SHAREPOINT_CLIENT_SECRET`), resolved live per call and never logged — there is no per-user
  entry. The minted access token is vaulted per-org as the connector's normal OAuth
  `TokenRecord` (encrypted at rest, never logged — AC5), so `get_connector_credentials()`
  resolves it identically to any other mode (AC3).
- **Scope.** The request uses `ConnectorAuthConfig.client_credentials_scopes`
  (`https://graph.microsoft.com/.default` for Graph) rather than the delegated `scopes` —
  the granted **application** permissions are resolved by Entra from the admin-consented app
  registration, not sent in the request. `oauth.get_client_credentials_token()` falls back to
  `scopes` when `client_credentials_scopes` is unset (SAP, D365, ServiceNow).
- **`get_token()` mints and re-mints.** client_credentials issues no `refresh_token`, so — like
  JWT bearer re-assertion — `get_token()` re-mints outbound on expiry (`_mint_client_credentials_token`),
  gated on the org's resolved mode being `client_credentials`. This is what keeps live ingestion
  running under a service identity with no user action (AC2).
- **Connect surface** (owner-only, no body): `POST /api/connectors/{connector_id}/client-credentials`.
  Token status / revoke reuse the standard OAuth `GET .../token-status` and `DELETE .../token`.
- **Setup:** application-permission app registration + tenant-wide admin consent — see
  `docs/INTEGRATE_GRAPH_CLIENT_CREDENTIALS.md`.

## Flows

- `authorization_code`: used by Salesforce, ServiceNow, Jira, Confluence, GitHub, Slack, Teams.
  Salesforce/ServiceNow/Jira/Confluence have a revocation endpoint; GitHub, Slack and Teams
  do not (Slack revokes via the `auth.revoke` Web API; Teams/Microsoft has no revocation
  endpoint, so revoke removes the vault token).
- `client_credentials`: used by SAP, Dynamic365, and — as an outbound-only alternative to
  authorization_code — Microsoft Teams / SharePoint (Graph); no `refresh_token`, no `redirect_uri`.

### Microsoft Graph connectors (Teams, SharePoint) — tenant admin consent

Teams and SharePoint authorise against Microsoft Entra ID (Azure AD) using the tenant in
`TEAMS_TENANT_ID` / `SHAREPOINT_TENANT_ID` (SharePoint defaults to the Teams tenant). Their
Graph scopes (`Sites.Read.All`, Teams channel-message read, `offline_access`) are
**admin-consent–required** application-directory permissions. This has two consequences worth
knowing before debugging a "connect keeps re-prompting" report:

- **Consent must be pre-granted at the tenant level by a directory admin.** If it has not
  been, every connect attempt re-shows the admin-approval prompt and a non-admin user can
  never complete the flow. Grant consent once for the Graph app registration (Azure portal →
  App registrations → API permissions → *Grant admin consent*), then normal users can connect.
- **Without granted consent, `offline_access` may not yield a `refresh_token`.** When Microsoft
  withholds the refresh token, the token-refresher job (`jobs/token_refresher.py`) has nothing
  to renew, so once the access token expires the connector drops to `needs_auth` and the user
  must re-run the OAuth flow. A connector that repeatedly lands in `needs_auth` after ~1 hour is
  the classic symptom of missing tenant admin consent — fix the consent grant, not the code.

A malformed `TEAMS_TENANT_ID` / `SHAREPOINT_TENANT_ID` (neither a directory GUID nor one of
`common` / `organizations` / `consumers`) is surfaced as a startup WARNING from `configs.py`,
since the authorize/token URLs are built from it at import time.

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

### Phase 2 — AgentIQ callback → frontend /oauth/callback (CS-2 / AT-325)

After the code exchange succeeds and the token is stored, the callback issues an internal redirect
to the frontend `/oauth/callback` page (`OAuthCallbackPage`), which then routes the user back to
Integration Hub. This redirect goes to `OAUTH_SUCCESS_REDIRECT` / `OAUTH_ERROR_REDIRECT` — it is
**not** sent to the OAuth provider.

- `OAUTH_SUCCESS_REDIRECT = '<base>/oauth/callback?connected={connector_id}&status=success'`
- `OAUTH_ERROR_REDIRECT   = '<base>/oauth/callback?status=error&code={error_code}'`
- `<base>` is `OAUTH_FRONTEND_BASE_URL` (server-controlled config, never request input). Blank by
  default → a relative path for same-origin / proxied deploys. Only `{connector_id}` and
  `{error_code}` are interpolated, both from server-side state — preserving open-redirect protection.

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
