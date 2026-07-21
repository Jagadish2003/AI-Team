# R18-A3 T3 — Microsoft Graph client-credentials (Teams / SharePoint)

AgentIQ 2.0 · Release 1.8 · Track A/D — Outbound-Initiated Connector Authentication

This is the setup reference for connecting **Microsoft Teams** and **SharePoint**
via the **client-credentials** grant — an **outbound-only, no-callback** path for
deployments that expose no public inbound HTTPS (`NETWORK_PROFILE=no_public_inbound`,
e.g. TCU and other security-conscious banks).

It covers the one thing this mode requires that the browser flow does not: an app
registration with **application permissions** and **tenant admin consent**.

---

## Why this mode exists (READ THIS FIRST)

The default Teams/SharePoint connect uses the **authorization_code** flow: a user
signs in through a browser, and Microsoft redirects **inbound** to AgentIQ's
callback URL to complete the connection. In a no-public-inbound deployment that
redirect can never arrive, so the connection fails at the last step.

**client-credentials** is the correct grant for a server reading data under a
**service identity**: AgentIQ authenticates as the *application itself* using the
app registration's `client_id` + `client_secret`, exchanged **outbound** for an
access token. There is no user sign-in, no redirect, and no inbound callback.

| | authorization_code (delegated) | client_credentials (application) |
| --- | --- | --- |
| Who the token represents | a signed-in user | the AgentIQ application (service identity) |
| Inbound callback needed | **Yes** (browser redirect) | **No** — outbound only |
| Graph permission type | Delegated | **Application** |
| Consent | user (+ admin for admin-scoped perms) | **tenant admin, one-time** |
| Token scope requested | granular (`ChannelMessage.Read.All`, …) | `https://graph.microsoft.com/.default` |
| Refresh token issued | yes (`offline_access`) | **no** — AgentIQ re-mints outbound on expiry |

Both modes read the same data and land the token in the same per-org vault record,
so **ingestion is unchanged** regardless of which mode an org uses.

---

## Prerequisites

- A Microsoft Entra ID (Azure AD) **app registration** in the customer tenant
  (the same one Teams/SharePoint already use for the browser flow can be reused).
- A **directory admin** (Global Administrator or Privileged Role Administrator) to
  grant tenant-wide consent — a one-time action.

---

## Permissions for the **standard** (browser / authorization_code) flow

> The rest of this doc is the **client-credentials** (no-public-inbound) path.
> If you connect Teams/SharePoint the normal way — `NETWORK_PROFILE=standard`,
> user signs in through the browser — you use the **authorization_code** flow, and
> the **delegated** permissions below still require a **one-time tenant admin
> consent**. This is the usual cause of a `403` on a fresh connect.

The browser flow requests exactly these **delegated** Microsoft Graph scopes
(read-only; defined in `backend/app/auth/configs.py`):

| Connector | Delegated Graph permissions | Graph reads they authorize |
| --- | --- | --- |
| Teams | `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All` | granted teams/channels + channel message content |
| SharePoint | `Sites.Read.All` | granted sites, their **pages, lists, document-library drives, driveItems, and file content** (deep content included) |

**All of these are admin-consent Graph permissions.** Even though a user signs in,
Microsoft returns `403` on the actual Graph calls until a tenant admin has granted
consent once (Azure portal → **Entra ID → Enterprise applications → [AgentIQ] →
Permissions → Grant admin consent**, or the `/adminconsent` endpoint below). A
`403` can also mean the **signed-in user has no access to that specific site/team**
— delegated permissions never exceed what the user themselves can see.

> `Sites.Read.All` is the ONLY SharePoint permission needed — it already covers
> site pages, lists, document libraries, and file-content downloads. Do **not** add
> `Files.*`, `Sites.Manage.All`, `Sites.FullControl.All`, or any `*.ReadWrite`
> scope; AgentIQ is read-only.

---

## Step 1 — Grant **application** Graph permissions

In the Azure portal → **Entra ID → App registrations → [AgentIQ app] → API
permissions → Add a permission → Microsoft Graph → _Application permissions_**, add
the **application** (not delegated) equivalents of the read-only scopes AgentIQ
uses:

| Connector | Application permissions (least privilege) |
| --- | --- |
| Teams | `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All` |
| SharePoint | `Sites.Read.All` |

> These are **read-only**. Do **not** add any `*.ReadWrite`, `*.Send`,
> `Sites.Manage.All`, `Sites.FullControl.All`, or `Chat.*` / `ChatMessage.*`
> permissions — AgentIQ never writes and never reads private chats/DMs. The
> application-permission set mirrors the delegated least-privilege scopes.

## Step 2 — Grant tenant admin consent

Still under **API permissions**, click **“Grant admin consent for [tenant]”**.
Application permissions **only take effect after admin consent** — until then every
Graph call returns `403`. This is a one-time tenant action; there is no per-connect
prompt (client-credentials has no interactive consent screen).

Alternatively, an admin can consent via the admin-consent endpoint:
`https://login.microsoftonline.com/{tenant}/adminconsent?client_id={client_id}`.

## Step 3 — Configure the deployment

The client-credentials mode reuses the existing app-registration configuration —
no new secrets beyond what Teams/SharePoint already need:

| Variable | Purpose |
| --- | --- |
| `TEAMS_CLIENT_ID` / `SHAREPOINT_CLIENT_ID` | App registration (client) id. `SHAREPOINT_CLIENT_ID` defaults to `TEAMS_CLIENT_ID` when the same app serves both. Non-secret. |
| `TEAMS_CLIENT_SECRET` / `SHAREPOINT_CLIENT_SECRET` | The app registration's client secret. **Required.** Stored/read via the vault-grade secret path; never logged. |
| `TEAMS_TENANT_ID` / `SHAREPOINT_TENANT_ID` | The **customer tenant GUID** (`SHAREPOINT_TENANT_ID` defaults to `TEAMS_TENANT_ID`). For client-credentials the token endpoint should target the **specific tenant GUID**, not `organizations`/`common`. |

The access token is always requested with the single resource scope
`https://graph.microsoft.com/.default`; the effective permissions are whatever the
app registration was granted and admin-consented in Steps 1–2.

## Step 4 — Connect (outbound, no callback)

An **Owner** connects each connector with a single call (no body — the credential
is the deployment secret, not a per-user entry):

```
POST /api/connectors/teams/client-credentials
POST /api/connectors/sharepoint/client-credentials
Authorization: Bearer <owner token>
```

Response:

```json
{ "connector_id": "teams", "connected": true, "auth_mode": "client_credentials" }
```

This performs the **outbound** token exchange, stores the token per-org in the
encrypted vault, and records the org's auth mode as `client_credentials`. No browser
redirect and no inbound callback are involved.

Token status and disconnect reuse the standard OAuth endpoints:

```
GET    /api/connectors/{teams|sharepoint}/token-status
DELETE /api/connectors/{teams|sharepoint}/token
```

---

## Token lifecycle

client-credentials tokens are short-lived (~1 hour) and Microsoft issues **no
refresh token** for this grant. AgentIQ handles this automatically: when a run
resolves the connector token (`get_token`) and the cached token is missing or near
expiry, it **re-mints outbound** from the app credentials. Ingestion therefore
continues indefinitely under the service identity with no user action — nothing
inbound is ever required.

> Note: the background token-refresher job only renews rows that carry a refresh
> token, so it deliberately skips client-credentials rows. Re-minting happens on
> read via `get_token`, which is what run-start credential resolution uses.

---

## Security posture

- The client secret is resolved live per call and **never logged**; a failed token
  request logs only Microsoft's `AADSTS...` error code, never the secret or body.
- The minted access token is **Fernet-encrypted at rest**, keyed per org, and never
  returned through any API or written to logs (AC5) — identical hygiene to every
  other connector credential.
- Nothing leaves the customer boundary: token acquisition is a direct outbound call
  from AgentIQ to Microsoft Entra. There is **no vendor relay** (Approach C is
  rejected — see the R18-A3 story).

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Connect returns `500 "Connector client secret is not configured."` | `TEAMS_CLIENT_SECRET` / `SHAREPOINT_CLIENT_SECRET` not set in the deployment env. |
| Connect returns `502` with `AADSTS700016` / `AADSTS7000215` | Wrong `client_id` / bad or expired client secret. |
| Connect succeeds but Graph reads return `403` | Admin consent not granted (Step 2), or the permissions were added as **delegated** instead of **application** (Step 1). |
| Ingest logs `Microsoft Graph GET .../sites/{id}/drives HTTP 403` (standard/browser flow) | `Sites.Read.All` not admin-consented for the app, **or** the signed-in user has no access to that SharePoint site. See "Permissions for the standard flow" above. The run degrades gracefully (SharePoint is skipped, other connectors continue). |
| Connect returns `400 "does not support the client-credentials auth mode"` | Called on a connector other than `teams` / `sharepoint` (only these register the Graph client-credentials mode). |

---

## Related

- Auth-mode abstraction and the outbound-only rationale: `backend/app/auth/README.md`.
- Deployment env-var reference: `deployment/README.md`.
- Story scope and acceptance criteria: R18-A3 (Outbound-Initiated Connector Authentication).
