# Scoped-Inbound OAuth Callback — Security-Team Deployment Package

AgentIQ 2.0 · Release 1.8 · R18-A3 (Outbound-Initiated Connector Authentication) · Task T6 / AC7

> **Audience:** the customer's **network / security team**. This is the review-and-negotiate
> package for the one case where AgentIQ needs a *narrowly scoped* inbound path in an
> otherwise no-public-inbound deployment. It is **documentation and configuration guidance
> only** — no AgentIQ code changes are required to adopt it. The inbound path is exposed and
> controlled entirely by **you**, on **your** infrastructure. AgentIQ never hosts, relays, or
> proxies any part of it.

---

## 1. TL;DR

For a small set of connectors that offer **no outbound-only authentication mode** (today:
**GitHub** and **Slack**), the only OAuth grant available is `authorization_code`, which finishes with the
identity provider redirecting a browser back to a **callback URL**. In a deployment that
exposes no public inbound HTTPS, that redirect has nowhere to land and the connect flow
fails at the last step.

**Approach B** makes exactly **one** URL path reachable from outside the network —
`GET /api/connectors/oauth/callback` — through a reverse proxy that you own, restricted to
allowlisted source ranges, and nothing else. Everything else about AgentIQ stays
outbound-only and internal.

This is the **negotiated fallback**. Prefer, in order:

1. **An outbound-only auth mode** for the connector (client-credentials / JWT bearer /
   static) — zero inbound. Most connectors already have one; see §3.
2. **Internal-only completion** (AUTH-2 model) — if the admin who runs the connect is
   **inside** the network, the browser redirect resolves against the internal deployment
   URL and **no public inbound is needed at all**. See §4.
3. **Approach B** — this document: a scoped, customer-controlled inbound path, only when 1
   and 2 cannot apply.

> ❌ **Approach C (a CloudFulcrum-hosted callback relay) is explicitly rejected** for
> boundary-sensitive deployments. Routing an auth artifact (the authorization `code`)
> through vendor infrastructure would place a vendor hop **inside your trust boundary** —
> the exact thing the no-data-leaves posture exists to prevent. The fallback is always a
> path **you** control, never a vendor relay. See §9.

---

## 2. Why this is needed (the last-step problem)

The `authorization_code` OAuth grant is designed for **user-delegated** access and has four
legs:

1. AgentIQ builds an authorize URL and the admin's browser goes to the provider — **outbound**.
2. The admin consents at the provider — **provider-side**.
3. The provider issues a `302` telling the **browser** to navigate to the registered
   `redirect_uri`, carrying `?code=…&state=…` — the **inbound** leg.
4. AgentIQ exchanges that `code` for a token at the provider's token endpoint — **outbound**.

Only **leg 3** needs an inbound-reachable URL. In a no-public-inbound deployment, leg 3
cannot arrive, so the connect never completes even though legs 1, 2, and 4 would all work.

For connectors that support an outbound-only mode, the correct fix is to **not use
`authorization_code` at all** (see §3). Approach B exists only for connectors where
`authorization_code` is the *only* option.

> **Key architectural fact — the callback is browser-delivered, not server-delivered.**
> Leg 3 is a `302` that the **admin's own browser** follows. The inbound request to the
> callback therefore originates from the **admin's browser egress IP**, *not* from the
> provider's servers. This makes the exposed surface far narrower than a typical webhook
> and directly shapes the allowlist in §6. AgentIQ has **no** server-to-server inbound
> webhooks — connector ingestion is entirely pull-based (outbound).

---

## 3. Which connectors actually need Approach B

A connector needs Approach B **only if it has no outbound-only auth mode** — i.e. its
supported modes contain none of `client_credentials`, `jwt_bearer`, or `static` (the set
`OUTBOUND_ONLY_MODES` in `backend/app/auth/auth_modes.py`). The auth-mode registry is the
**single source of truth**, so this list stays correct as connectors gain new modes — do
not hard-code it elsewhere.

| Connector | Supported modes today | Outbound-only option? | Needs Approach B? |
|---|---|---|---|
| Salesforce / nCino | `authorization_code`, `jwt_bearer` | ✅ JWT bearer | **No** — use JWT bearer |
| ServiceNow | `authorization_code`, `client_credentials`, `static` | ✅ client-credentials / static | **No** |
| Jira | `authorization_code`, `static` | ✅ API token (static) | **No** |
| Confluence | `authorization_code`, `static` | ✅ API token (static) | **No** |
| SAP | `client_credentials` | ✅ client-credentials | **No** |
| Dynamics 365 | `client_credentials` | ✅ client-credentials | **No** |
| Native DBs (Oracle / PostgreSQL / SQL Server) | `static` | ✅ direct outbound + vault creds | **No** |
| **GitHub** | `authorization_code` | ❌ none | **Yes** |
| **Slack** | `authorization_code` | ❌ none | **Yes** |
| Microsoft Teams | `authorization_code`, `client_credentials` | ✅ client-credentials (AT-556) | **No** — use Graph client-credentials |
| SharePoint | `authorization_code`, `client_credentials` | ✅ client-credentials (AT-556) | **No** — use Graph client-credentials |

Confirm the live list for your build with the registry rather than this table:

```python
# From backend/, with the venv active:
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.auth_modes import OUTBOUND_ONLY_MODES
needs_b = [
    cid for cid, cfg in CONNECTOR_AUTH_CONFIGS.items()
    if not (set(cfg.supported_auth_modes) & OUTBOUND_ONLY_MODES)
]
print(sorted(needs_b))   # connectors with authorization_code ONLY → need Approach B
```

> If a connector in your deployment plan is **not** in the "needs Approach B" set, do **not**
> open an inbound path for it — switch it to its outbound-only mode instead (Integration Hub →
> the connector's outbound setup path). Approach B is a last resort, per connector.

---

## 4. First choice for no-inbound: internal-only completion (AUTH-2 model)

Because the callback is **browser-delivered** (§2), a connect can often complete with **no
public inbound at all**:

- If the admin performing the connect is **on the internal network or VPN**, their browser
  can reach the internal deployment URL directly. The provider's `302` sends the browser to
  `OAUTH_REDIRECT_URI`; as long as that host resolves **inside** the network, leg 3 lands
  without any public exposure.
- This is the same property AUTH-2 relies on for org-approval email links: **clicked from
  inside the network, they resolve against the internal deployment URL.**

**Try this before Approach B.** If every admin who will ever connect a `authorization_code`
connector does so from inside the network, you need **no** inbound path — register
`OAUTH_REDIRECT_URI` as the internal URL and stop here.

Approach B is required only when that cannot be guaranteed — e.g. admins who must connect
from outside the network, or a provider that requires the `redirect_uri` to be a
publicly-resolvable HTTPS host it can validate.

---

## 5. The exposed surface — exactly one path

Approach B exposes a **single** path inbound. Everything below is the complete contract of
what the reverse proxy must (and must not) allow.

| Property | Value |
|---|---|
| **Path** | `/api/connectors/oauth/callback` — exact match, no prefix wildcard |
| **Method** | `GET` only |
| **Query params** | `code`, `state`, `error` (standard OAuth) — must be forwarded verbatim |
| **Scheme** | HTTPS only (valid TLS cert on the public hostname) |
| **Body** | none (GET) — reject any request with a body |
| **Everything else** | **not** publicly reachable — all other backend/API/UI paths stay internal |

`OAUTH_REDIRECT_URI` (see §8) must be set to the **public** form of this path, e.g.
`https://agentiq-callback.customer.example/api/connectors/oauth/callback`, and that same URL
must be registered as the redirect/callback URL in each affected provider's OAuth app.

After AgentIQ processes the `code`, it issues its own `302` back to the frontend
`/oauth/callback` page (a normal in-app navigation). That subsequent redirect resolves
however the admin normally reaches the AgentIQ UI — it does **not** require a second public
path when the admin is internal.

---

## 6. Source-IP allowlist

Deny by default; allow only what leg 3 actually needs. Because the callback is
browser-delivered, the source you allow is the **admin's browser egress**, not the
provider — this is the tightest possible allowlist.

### 6a. Primary allowlist — admin browser egress (required)

Allow only the egress ranges the connecting admins actually come from:

- The **corporate NAT / outbound egress CIDR** of the office(s) admins connect from, and/or
- The **VPN egress CIDR** if admins connect over VPN.

For a deployment where all connects happen from inside the network (§4), this can be the
internal range only — in which case the "public" path is really only reachable from your own
egress, which is the ideal end state.

### 6b. Provider published ranges (only if you use a server-delivered callback)

AgentIQ's OAuth callback is browser-delivered, so **provider IP ranges are not normally the
allowlist source.** Include them only if your topology deliberately routes a provider
server-side call to this path. When you do need them, source them from the provider's
**official, machine-readable** range feeds — never a hand-maintained list:

| Provider | Where to source current ranges | Notes |
|---|---|---|
| **GitHub** | `GET https://api.github.com/meta` → `hooks` / `web` / `api` CIDR arrays; docs: "About GitHub's IP addresses" | Ranges change; pull programmatically and refresh. |
| **Microsoft (Entra / Teams / SharePoint / M365)** | "Office 365 URLs and IP address ranges" web service + Azure **service tags** | Microsoft **recommends FQDN/URL allowlisting over IP** — ranges are large and dynamic. Prefer URL allowlisting where your proxy supports it. |
| **Slack** | Slack does **not** publish OAuth-redirect IPs and advises against IP-allowlisting OAuth | Slack publishes ranges only for the Events API / outgoing webhooks, which AgentIQ does not use. Rely on 6a for Slack. |

> **Do not** paste static IP lists into the proxy config and forget them. Provider ranges
> drift; a stale allowlist silently breaks connects. If you must allow provider ranges,
> automate the refresh from the feeds above.

---

## 7. Reverse-proxy configuration patterns

The pattern is identical across proxies: **exact-match the callback path, allow only §6
sources, allow only `GET`, forward the query string verbatim, refuse everything else.** TLS
terminates at the proxy; the backend stays on the internal network.

### 7a. nginx

```nginx
# Public listener — the ONLY thing reachable from outside the network.
server {
    listen 443 ssl;
    server_name agentiq-callback.customer.example;

    ssl_certificate     /etc/ssl/certs/agentiq-callback.crt;
    ssl_certificate_key /etc/ssl/private/agentiq-callback.key;

    # --- The one exposed path: OAuth callback, exact match, GET only ---------
    location = /api/connectors/oauth/callback {
        # (§6a) Admin browser egress — the real source of the browser 302.
        allow 203.0.113.0/24;        # <-- corporate / VPN egress CIDR(s)
        # allow <provider CIDR>;     # (§6b) ONLY if a server-delivered callback is used
        deny  all;                   # deny-by-default

        # Method allowlist — the callback is GET only.
        limit_except GET { deny all; }

        # Forward to the INTERNAL backend, preserving ?code=&state=.
        proxy_pass         http://agentiq-backend.internal:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $remote_addr;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_pass_request_body off;         # GET carries no body
    }

    # Everything else inbound is refused — no other path is publicly reachable.
    location / {
        return 404;
    }
}
```

### 7b. Apache (httpd)

```apache
<VirtualHost *:443>
    ServerName agentiq-callback.customer.example
    SSLEngine on
    SSLCertificateFile      /etc/ssl/certs/agentiq-callback.crt
    SSLCertificateKeyFile   /etc/ssl/private/agentiq-callback.key

    # Refuse everything by default.
    <Location "/">
        Require all denied
    </Location>

    # Allow only the OAuth callback path, GET only, from allowlisted sources.
    <Location "/api/connectors/oauth/callback">
        <LimitExcept GET>
            Require all denied
        </LimitExcept>
        Require ip 203.0.113.0/24        # <-- corporate / VPN egress CIDR(s)
        ProxyPass        http://agentiq-backend.internal:8000/api/connectors/oauth/callback
        ProxyPassReverse http://agentiq-backend.internal:8000/api/connectors/oauth/callback
    </Location>
</VirtualHost>
```

### 7c. Cloud WAF / API gateway (AWS API Gateway, Azure Front Door, Cloudflare)

Same invariants, expressed as gateway rules:

1. **Route allowlist:** publish only `GET /api/connectors/oauth/callback`; leave every other
   path unrouted (default deny / 404).
2. **Method restriction:** deny non-`GET` on that route.
3. **IP allowlist:** attach a WAF IP-set of the §6a egress ranges (add §6b provider ranges
   only if server-delivered). Cloudflare: an IP Access Rule scoped to the callback path.
4. **TLS:** managed cert on the public hostname; origin (the backend) stays private.
5. **Query string:** ensure the gateway forwards `code`/`state` unmodified (do not strip or
   cache query strings on this route).

---

## 8. Configuration reference (env)

These are set on the AgentIQ deployment; none is a code change. See `deployment/README.md`
for the full env reference.

| Variable | Set to | Why |
|---|---|---|
| `OAUTH_REDIRECT_URI` | `https://<public-callback-host>/api/connectors/oauth/callback` | The public callback URL. Must equal the redirect URL registered in each affected provider's OAuth app. |
| `OAUTH_FRONTEND_BASE_URL` | the frontend origin (blank if same-origin/proxied) | Where the backend redirects the browser after the callback (`/oauth/callback?…`). Internal is fine when admins are internal. |
| `OAUTH_STATE_SECRET` | `openssl rand -hex 32` | HMAC secret that signs the `state` param (tamper-evident org binding). Must be **identical** on the instance that issues the authorize URL and the one that handles the callback. Keep separate from `JWT_SECRET`. |
| `ENVIRONMENT` | `production` | Enforces `JWT_SECRET` and **force-disables** the dev-only unauthenticated-callback bypass (see the security note below). |

> ⚠️ **Never set `OAUTH_CALLBACK_ALLOW_UNAUTH` in a shared/staging/production deployment.**
> It is a local-dev convenience that disables the callback's Bearer requirement; it is
> **force-ignored when `ENVIRONMENT=production`** (logged at WARNING). Approach B does not
> need it — the callback's real CSRF/tenant defence is the signed, single-use `state`
> nonce, which is always on (§10).

---

## 9. Why not a vendor-hosted relay (Approach C)

A hosted relay would have CloudFulcrum receive the provider's callback and forward the
authorization `code` into the customer network. It is **rejected on principle** for
boundary-sensitive deployments:

- The authorization `code` is a short-lived **auth artifact**. Routing it through vendor
  infrastructure puts a **vendor hop inside the customer's trust boundary** — precisely
  what the "nothing leaves the boundary" posture that wins these deals forbids.
- Approach B keeps the callback path on **customer-controlled** infrastructure end to end.
  The customer's security team owns the proxy, the cert, the allowlist, and the logs.

The fallback is always a scoped inbound path the **customer** controls — never a vendor relay.

---

## 10. What AgentIQ already enforces on the callback (defence in depth)

Approach B is a network control layered **on top of** application controls that are always
on. The scoped inbound path is not the only thing standing between the internet and a token
— it narrows the surface; these enforce correctness on whatever reaches it:

- **HMAC-signed `state`** — carries the initiating org + a nonce, signed with
  `OAUTH_STATE_SECRET`. A tampered or forged `state` (e.g. an org-id swapped to another
  tenant) fails signature verification and is rejected with a generic `400` before any token
  work.
- **Single-use, TTL-bounded nonce** — server-side, 10-minute window, deleted on first use.
  Replays and reused/refreshed authorize URLs fail closed.
- **Tenant binding** — the org in the signed `state` must match the org bound to the nonce
  at initiation; a mismatch is refused. There is **no** hardcoded default-org fallback.
- **PKCE (S256)** — a per-request verifier is bound to the nonce and its challenge sent on
  the authorize URL, for providers that enforce PKCE.
- **Bearer on the callback** — required in production; the dev-only bypass is force-disabled
  when `ENVIRONMENT=production` (§8).
- **TLS-only** and **credentials encrypted at rest** — the exchanged token lands in the
  per-org Fernet-encrypted vault, write-only, never logged (same hygiene as every other
  credential).

The reverse proxy adds **network-layer** scoping (path, method, source IP); the application
adds **request-layer** integrity (signature, single-use, tenant match). Both together are
the package.

---

## 11. Security-team review checklist

Hand this list to the reviewing security team; every item is satisfiable with the config in
this document.

- [ ] Exactly **one** inbound path is published: `GET /api/connectors/oauth/callback`. All
      other paths return 404 from outside (§5, §7).
- [ ] The path accepts **`GET` only**; other methods are denied (§7).
- [ ] Source IPs are **deny-by-default**; only admin egress (and, if truly needed,
      auto-refreshed provider ranges) are allowed (§6).
- [ ] TLS terminates at the customer-controlled proxy with a valid cert; the backend origin
      stays on the internal network (§7).
- [ ] `OAUTH_REDIRECT_URI` matches the registered provider redirect URL and points at the
      public callback path (§8).
- [ ] `OAUTH_STATE_SECRET` is set (and identical across issuing/handling instances);
      `ENVIRONMENT=production`; `OAUTH_CALLBACK_ALLOW_UNAUTH` is unset (§8).
- [ ] The path is used **only** for connectors with no outbound-only mode; all others use
      their outbound-only mode (§3).
- [ ] No vendor relay is involved — the inbound path is entirely customer-controlled (§9).
- [ ] Application-layer defences (signed single-use `state`, tenant binding, PKCE) are
      confirmed present (§10).

---

## 12. Verification

Once the proxy and env are in place, verify the scoping before connecting a real provider:

```bash
# 1. The callback path is reachable via GET from an allowlisted source (a missing
#    code/state yields AgentIQ's error redirect — proves the path is wired, not open).
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://<public-callback-host>/api/connectors/oauth/callback"      # expect 302 (error redirect) or 400

# 2. A non-GET method on the callback path is refused by the proxy.
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "https://<public-callback-host>/api/connectors/oauth/callback"      # expect 405/403

# 3. Any OTHER path is not publicly reachable.
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://<public-callback-host>/api/runs/start"                     # expect 404/403

# 4. From a NON-allowlisted source, even the callback path is refused.
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://<public-callback-host>/api/connectors/oauth/callback"      # expect 403 from outside the allowlist
```

Then run one real end-to-end connect for an affected connector (e.g. GitHub or Slack) from
an allowlisted admin browser and confirm the token is stored and ingestion runs — with all
**other** inbound paths still refused.

---

## 13. Related references

- `backend/app/auth/README.md` — the auth-mode abstraction and the four modes.
- `backend/app/auth/auth_modes.py` — `OUTBOUND_ONLY_MODES` and the per-connector supported-mode
  registry (source of truth for §3).
- `backend/app/routes_connector_auth.py` — the `/api/connectors/oauth/callback` handler and its
  state/nonce/PKCE/tenant-binding checks (§10).
- `deployment/README.md` — full environment-variable reference (§8) and the reverse-proxy
  rate-limiting notes.

---

*Package owner: Track A — Connectors & Enterprise Technology. This is a customer-facing
security-negotiation artifact (R18-A3 T6 / AC7). Keep the connector table in §3 in step with
the auth-mode registry when a connector gains or loses an outbound-only mode.*
