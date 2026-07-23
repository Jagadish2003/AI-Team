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

## Instance-Only Environment Policy (R17-D3 Addendum A, T13 / AC12)

`backend/.env` (and the tracked `backend/.env.example` / `backend/.env.template`)
hold **instance configuration only** — things legitimately per-deployment, never
per-client. Per-client connector credentials live exclusively in the per-org
Fernet-encrypted credential vault (the `credentials` table), entered through the
Integration Hub: OAuth Connect for Salesforce/Jira/ServiceNow/etc., or the
static-credential form (Owner role, write-only) for Jira API tokens, ServiceNow
user/password, and native DB connection credentials.

**Retained in env (instance-only):**

| Category | Variables |
|---|---|
| Database | `DATABASE_URL` (+ `DEV_/PROD_/TEST_DATABASE_URL` selectors) |
| Vault key | `CREDENTIAL_VAULT_KEY` — deliberately stays in env: it encrypts the vault's contents, so storing it in the database it protects would be circular. Env or the customer's secrets manager only. |
| CORS / server | `CORS_ORIGINS`, `DEV_JWT`, `JWT_SECRET`, `OAUTH_STATE_SECRET`, `ENVIRONMENT` |
| OAuth app registrations | `OAUTH_REDIRECT_URI`, `OAUTH_FRONTEND_BASE_URL`, `{CONNECTOR}_CLIENT_ID` / `{CONNECTOR}_CLIENT_SECRET` (the deployment's OAuth **app**, not any client's token), tenant IDs |
| Model gateway | `ANTHROPIC_API_KEY`, `MODEL_*_PROVIDER`, `IN_BOUNDARY_*`, `CUSTOMER_TENANT_*` (dev fallback only — production customer-tenant key is vaulted) |
| Email / licensing / flags | `SMTP_*`, `EMAIL_*`, `LICENSE_*`, feature flags (e.g. `INFERRED_RELATIONSHIPS_ENABLED`) |
| Native DB connectors (interim) | `ORACLE_*` / `POSTGRESQL_*` / `SQLSERVER_*` service-account vars — still env-resolved by the DB driver layer until its vault wiring lands; single-tenant/standalone use only |

**Removed (per-client — must never return to env):** `SF_ACCESS_TOKEN`,
`SF_CLIENT_ID`, `SF_USER`, `JIRA_URL`, `JIRA_USER`, `JIRA_TOKEN`,
`SERVICENOW_URL`, `SERVICENOW_USER`, `SERVICENOW_PASS`, `SERVICENOW_TOKEN`,
`NCINO_INSTANCE_URL`, `NCINO_ACCESS_TOKEN`, `STRS_INSTANCE_URL`,
`STRS_ACCESS_TOKEN`. Environment variables are process-global, so an env
credential can never be per-org — two orgs on one instance would share it.
`backend/tests/unit/test_env_no_per_client_credentials.py` enforces this for
the tracked templates.

### Migrating an existing deployment (R17-D3 Addendum A, T15 / AC14)

A pre-Addendum install that already has working per-client credentials in
`backend/.env` uses a **one-time, explicit** admin command to move them into the
per-org vault before those vars are removed. It is never run automatically —
explicit operator action is required so you always know where the credentials
now live.

```bash
cd backend
# 0. Ensure the schema carries the T10 static-credential columns first:
alembic upgrade head            # or apply database/provision/provision.sql
# 1. Preview what would migrate (writes nothing):
python scripts/migrate_env_credentials_to_vault.py --dry-run
# 2. Migrate the legacy env credentials into the vault for the instance's org:
python scripts/migrate_env_credentials_to_vault.py --org <org_id>
```

- Reads whatever legacy vars are set (`SF_*`, `JIRA_*`, `SERVICENOW_*`,
  `NCINO_*`, `STRS_*`) and stores each as a Fernet-encrypted **static
  credential** under `--org` (default: the instance's default org). ServiceNow
  migrates its OAuth bearer token if set, otherwise its user/password.
- **Exactly once:** a connector already present in the vault is **skipped** —
  re-running never silently clobbers a credential connected since the first run.
  Use `--force` only to deliberately overwrite.
- Requires `CREDENTIAL_VAULT_KEY`; it preflights the vault key and the schema and
  exits with a clear message if either is missing. It never prints a secret.
- On success it prints the exact list of env vars to delete from `backend/.env`.
  Remove them, then reconnect Salesforce via OAuth Connect if you want its token
  to auto-refresh (the migrated static token works but does not refresh).

## OAuth Connector Secrets

Each connector's client secret is resolved from the environment at runtime via `secret_key`
in `ConnectorAuthConfig`. No secret value is ever stored in code.

Set the following variables in your deployment environment (or `.env` for local dev).
See `backend/.env.template` for the full list including non-secret client IDs.

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
| Microsoft Teams | `TEAMS_CLIENT_SECRET`      | authorization_code / client_credentials |
| SharePoint      | `SHAREPOINT_CLIENT_SECRET`   | authorization_code / client_credentials |

Set each to `your_secret_here` as a placeholder; replace with the real value from the
provider's OAuth app registration before going to production.

### Other required secrets

| Variable                | Purpose                                         |
|-------------------------|-------------------------------------------------|
| `CREDENTIAL_VAULT_KEY`  | Fernet key for encrypting tokens at rest        |
| `OAUTH_REDIRECT_URI`    | Provider callback URL — must point at `…/api/connectors/oauth/callback` and be registered with every authorization_code provider |
| `OAUTH_FRONTEND_BASE_URL` | Frontend origin the backend redirects to after the OAuth callback (CS-2 / AT-325). Backend appends `/oauth/callback?connected=…&status=success` or `/oauth/callback?status=error&code=…`. Leave blank for relative (same-origin / proxied) deploys; set to the frontend origin when FE/BE differ |
| `DEV_JWT`               | Bearer token for local dev auth (`dev-token-change-me` by default) |
| `JWT_SECRET`            | HS256 signing secret for user-login JWTs (AUTH-1). **Required in production** — issuance fails closed if unset when `ENVIRONMENT=production`. Generate with `openssl rand -hex 32`. |
| `OAUTH_STATE_SECRET`    | Dedicated HMAC secret for signing the OAuth `state` parameter (R17-D3 / AT-447). Keep **separate** from `JWT_SECRET` so rotating the session key does not invalidate in-flight OAuth flows. Falls back to `JWT_SECRET` if unset. Generate with `openssl rand -hex 32`. |
| `ENVIRONMENT`           | `production` enforces `JWT_SECRET`, fail-closed invite behaviour, and force-disables the dev-only OAuth bypass flags below; unset for dev/test. |

> ⚠️ **Dev-only security bypass — never set in production:** `OAUTH_CALLBACK_ALLOW_UNAUTH`
> lets the OAuth callback complete without a Bearer token (a provider's browser redirect
> carries none), which is a local-dev convenience only. It **disables the callback's
> tenant-binding auth**, so it must never be enabled in a shared/staging/production
> deploy. It is **force-ignored when `ENVIRONMENT=production`** (logged at WARNING), and
> CI sets it to empty. Likewise the `X-Org-Id` header is honoured as an org fallback
> **only outside production** — in production tenant context comes solely from the signed
> JWT org claim. Leave both unset in any shared environment.

### Notes

- Jira and Confluence share a single Atlassian OAuth app. Set `ATLASSIAN_CLIENT_ID` and
  use separate `JIRA_CLIENT_SECRET` / `CONFLUENCE_CLIENT_SECRET` values.
- SharePoint (R17-A2 / AT-462) reuses the **Teams** Microsoft Graph app registration:
  `SHAREPOINT_CLIENT_ID` defaults to `TEAMS_CLIENT_ID` and `SHAREPOINT_TENANT_ID` defaults
  to `TEAMS_TENANT_ID`, so a single Graph app can serve both. Set them only when SharePoint
  uses a dedicated app/tenant. The per-connector `SHAREPOINT_CLIENT_SECRET` is still required
  (framework convention) — set it to the shared Teams app's secret when reusing that app.
  SharePoint requests minimal read-only Graph scopes (`Sites.Read.All`, `offline_access`).
- SAP and Dynamics 365 use client_credentials flow — no OAuth redirect URI is needed.
- **Microsoft Teams / SharePoint outbound-only (R18-A3 T3 / AT-556):** in addition to the
  browser authorization_code flow, both connectors support a **client_credentials** mode
  that authenticates the Graph app registration under a *service identity* — outbound-only,
  with **no callback**, for `NETWORK_PROFILE=no_public_inbound` deployments (e.g. TCU). It
  reuses the same `TEAMS_CLIENT_SECRET` / `SHAREPOINT_CLIENT_SECRET` app secret; the app
  registration must be granted **application** (not delegated) Graph permissions with
  tenant-wide **admin consent**, and the token is requested with the `https://graph.microsoft.com/.default`
  scope. An Owner connects with `POST /api/connectors/{teams|sharepoint}/client-credentials`
  (no body). **Full admin-consent setup: [`docs/INTEGRATE_GRAPH_CLIENT_CREDENTIALS.md`](../docs/INTEGRATE_GRAPH_CLIENT_CREDENTIALS.md).**
- Slack revocation uses the `auth.revoke` Web API (not RFC 7009). No extra config required.
- **Deploy note (R17-D3):** connector tokens are now stored under the authenticated
  org instead of a hardcoded `default` org. Any connector tokens stored under `default`
  by a pre-R17-D3 build are orphaned — connectors connected before the upgrade will show
  as **disconnected** and must be reconnected once. No data is lost; the stale rows are
  simply not read under the real org. Communicate this one-time reconnect to early-access
  customers when deploying this change.

### Native AWS event connector (`aws_events`, MSP-B1)

The native AWS connector (CloudWatch alarm history, EventBridge, CloudTrail) uses
**cross-account role assumption from a hub identity** — one connection, many
accounts, each account a scope. It is configured, not discovered, and no AWS
secret ever lives in `.env` or config.

| Setting | Purpose |
|---|---|
| `AWS_EVENT_ACCOUNTS` | JSON array of secret-free managed-account configs: `{account_id, role_arn?, external_id?, regions[], partition?}`. Role ARNs/regions/external id are non-secret and live here (or offline uses the fixture); an account with a `role_arn` is reached by role assumption, one without by direct keys. Inline AWS keys are rejected. |
| `partition` (per account) | `aws` (commercial, default) or `aws-us-gov` (GovCloud). Selectable per connection; when omitted it is derived from the account's region, and a region that contradicts the partition (a GovCloud region under commercial, or vice-versa) is rejected at config time. GovCloud resolves `*.us-gov-west-1.amazonaws.com` endpoints and `arn:aws-us-gov:…` ARNs. Live GovCloud verification (incl. FIPS endpoints) is MSP-B9's follow-through. |
| Vault: `aws_events` | The **hub** identity's access key (username = access key id, secret = secret access key), Fernet-encrypted in the credential vault. |
| Vault: `aws_events:account:{account_id}` | Optional **direct per-account read-only keys** — the fallback used when an account offers no cross-account role (or role assumption fails). |
| `AWS_EVENTS_HUB_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` / `_SESSION_TOKEN` | CLI/standalone hub-key fallback **only** — never production, never in `.env` templates. |

Per managed account the hub calls `sts:AssumeRole` on the account's read-only
role (ExternalId-gated) for short-lived scoped credentials. The **minimal
read-only IAM policy** AWS reviewers should inspect before granting access is the
partner artifact [`aws_readonly_iam_policy.json`](./aws_readonly_iam_policy.json) +
[`AWS_READONLY_IAM_POLICY.md`](./AWS_READONLY_IAM_POLICY.md) — exactly the calls
the connector makes (`cloudwatch:DescribeAlarmHistory`, `events:ListRules`/`DescribeRule`,
`cloudtrail:LookupEvents`, `sts:AssumeRole`), no wildcard or write actions.
`boto3` is a lazy, live-only dependency (offline runs and tests never import it).

---

## No-Public-Inbound Deployments (R18-A3)

Security-conscious deployments (e.g. banks) often expose **no public inbound HTTPS** at all.
AgentIQ is designed to run outbound-only in that posture: connector ingestion is entirely
pull-based, and most connectors authenticate with an **outbound-only auth mode**
(client-credentials, JWT bearer, or a static vault credential) that never needs a provider
to redirect back into the network. See `backend/app/auth/README.md` for the auth-mode
abstraction and `OUTBOUND_ONLY_MODES`.

### `NETWORK_PROFILE` deployment flag (R18-A3 T5 / AT-558)

A deployment declares its inbound-network posture with one env var:

| Value | Meaning |
|---|---|
| `standard` (default) | The deployment can accept inbound HTTPS; the browser authorization-code OAuth flow (provider redirect → callback) completes normally. Nothing changes. |
| `no_public_inbound` | The deployment exposes **no public inbound HTTPS**. The Integration Hub then **hides the authorization-code Connect button** for every connector that has an outbound-only mode configured, and shows the **outbound setup path** instead (JWT-bearer key entry for Salesforce, client-credentials connect for Microsoft Graph / ServiceNow, or the static-credential form). The customer can never start a flow that cannot complete (AC4). |

Anything unset or unrecognised falls back to `standard` — an unknown value must never
silently hide connect flows.

The backend exposes the flag plus a per-connector auth-capability map at
`GET /api/network-profile` (viewer+), which the frontend pairs with each connector's
`has_outbound_only_mode` to decide, per tile, whether to offer the browser flow or the
outbound setup path. The flag lives at the connect/setup edge only — it never touches
ingestion (which stays mode-agnostic via `get_connector_credentials()`).

Connectors whose **only** grant is `authorization_code` (**GitHub**, **Slack** — and
Teams / SharePoint before their client-credentials mode shipped) have no outbound-only
mode, so their Connect button is **not** hidden. Those fall back to the two options below.

The one exception is a connector whose **only** OAuth grant is `authorization_code`
(currently **GitHub** and **Slack**, plus **Teams / SharePoint** until their client-credentials
mode ships). That grant finishes with a browser redirect to a callback URL, which needs an
inbound-reachable path. Two options, in order of preference:

1. **Internal-only completion (zero inbound).** Because the callback is browser-delivered, an
   admin **inside** the network completes the flow against the internal deployment URL with no
   public inbound at all — the same property AUTH-2 approval links rely on. Set
   `OAUTH_REDIRECT_URI` to the internal URL.
2. **Scoped-inbound fallback (Approach B).** When internal-only completion cannot be
   guaranteed, expose **only** `GET /api/connectors/oauth/callback` through a
   customer-controlled reverse proxy, restricted to allowlisted source ranges. This is the
   package a customer's security team reviews and negotiates:

   **→ See [`deployment/SCOPED_INBOUND_CALLBACK.md`](SCOPED_INBOUND_CALLBACK.md)** for the
   full reverse-proxy patterns (nginx / Apache / cloud WAF), the source-IP allowlist
   guidance, the exact exposed surface, the application-layer defences already enforced on
   the callback, and a security-team review checklist.

> A vendor-hosted callback relay (Approach C) is **rejected** for boundary-sensitive
> deployments — it would route an auth artifact through vendor infrastructure. The fallback
> is always a path the **customer** controls. Details in the linked package.

### AUTH-2 org-approval email links in no-inbound environments (R18-A3 T7)

AUTH-2 sends the CloudFulcrum/deployment admin an email with **Approve** and **Reject**
links when a new organisation registers. These links work in a no-public-inbound
deployment **without any inbound exposure**, because — unlike an OAuth provider callback,
which the *provider* initiates inbound from the internet — an approval link is clicked by an
**internal admin whose browser is already inside the deployment network**. The request is
outbound *from the admin's browser to the internal deployment*, never inbound from the
public internet.

Two properties make this hold, and both are already built in — nothing hits a dead flow:

- The **email links are built from `AGENTIQ_BACKEND_URL`** (`app/email_service.py`
  `send_org_approval_request_email`). Set it to the **internal deployment URL** (e.g.
  `https://agentiq.internal.bank.local`) and every approve/reject link resolves against the
  internal host.
- The GET link renders a **confirmation page whose form POSTs to a relative,
  same-host path** (`/api/auth/org-approval/{approve,reject}` — see `routes_auth.py`
  `_confirmation_page`). The state-changing POST therefore lands on whatever internal host
  served the page; the commit step never depends on an absolute or public URL. (The GET is
  deliberately non-mutating so email security scanners can't pre-approve an org.)

The follow-on **“organisation approved”** email link (`login_url`) is built from
**`PUBLIC_HOSTNAME`**; set that to the internal deployment URL too so the newly approved
registrant lands on the internal login page.

**Configuration for no-inbound deployments — set both to the internal deployment URL:**

| Variable | Used for | No-inbound value |
|---|---|---|
| `AGENTIQ_BACKEND_URL` | AUTH-2 approve/reject links (backend action links) | Internal deployment URL (e.g. `https://agentiq.internal.bank.local`) |
| `PUBLIC_HOSTNAME` | login / reset-password / invite / approved-email links | Internal deployment URL |

**Limitation to communicate to the customer:** approving or rejecting an organisation
requires an admin **with network access to the deployment** (VPN or on-network) — this is by
design, and it is the *only* AUTH-2 constraint added by the no-inbound posture. If
`AGENTIQ_BACKEND_URL` / `PUBLIC_HOSTNAME` are left at their `localhost` defaults, or pointed
at a public host the internal admin cannot reach, the links will not resolve; point them at
the internal deployment URL. No inbound firewall rule is required.

---

## MSP Connector Partner Security Artifacts

Cloud connectors that read a customer's estate ship a **least-privilege, read-only**
access artifact a customer security team reviews before granting access — the same
posture per provider.

- **Azure Event Connector (MSP-B2)** — the minimal, read-only Azure custom RBAC role
  (Alerts + Administrative Activity Log + Service Health reads only; no write/delete/
  management/metrics/Log Analytics/Defender/Sentinel). Works for both Azure Lighthouse
  delegated access and direct per-subscription assignment; identical in AzureCloud and
  AzureUSGovernment.

  **→ See [`deployment/AZURE_EVENT_CONNECTOR_RBAC.md`](AZURE_EVENT_CONNECTOR_RBAC.md)**
  for the permission-by-capability mapping, exclusions, `az` deployment steps,
  Lighthouse/direct guidance, and the security-review checklist. The importable role
  definition is [`deployment/azure_event_connector_role.json`](azure_event_connector_role.json).

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
