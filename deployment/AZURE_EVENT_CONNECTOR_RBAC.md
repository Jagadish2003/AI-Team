# Azure Event Connector — Minimal Reader RBAC Role (Partner Security Artifact)

AgentIQ 2.0 · MSP Readiness · Track A · **MSP-B2 T5 (AT-652)**

This is the partner-facing security artifact for the **AgentIQ Azure Event
Connector** (MSP-B2). It defines the **minimal, read-only** Azure custom RBAC role
the connector needs and how to assign it — the Azure counterpart to the AWS
connector's IAM policy document (MSP-B1). The machine-readable role definition ships
alongside this doc as [`azure_event_connector_role.json`](azure_event_connector_role.json).

The role follows the **principle of least privilege**: it grants exactly the three
read actions the connector calls and nothing else. It is suitable for a customer
security-team review before delegation.

---

## Purpose

The Azure Event Connector is **outbound polling only** (no Event Grid subscriptions,
no webhooks, no inbound listeners — honoured under `NETWORK_PROFILE=no_public_inbound`).
It authenticates as a vaulted **service principal** (client-credentials → ARM token)
and, per **pinned** subscription, reads three operational event classes. This role
grants read access to exactly those three surfaces.

## Supported connector features → required permission

Every permission maps back to exactly one connector capability. There are **no
unused permissions**.

| Connector capability (V1 scope) | ARM surface the connector calls | Required action |
|---|---|---|
| Azure Monitor **Alerts** (fired alert instances) | `GET …/providers/Microsoft.AlertsManagement/alerts` | `Microsoft.AlertsManagement/alerts/read` |
| Azure **Activity Log** — **Administrative** events (audit stream) | `GET …/providers/Microsoft.Insights/eventtypes/management/values` | `Microsoft.Insights/eventtypes/values/read` |
| Azure **Service Health** events | `GET …/providers/Microsoft.ResourceHealth/events` | `Microsoft.ResourceHealth/*/read` |

These are the three actions named in the MSP-B2 connector design (§1, "Auth &
access") and are exactly what the implementation invokes
(`backend/discovery/ingest/azure_alerts.py`, `azure_admin_events.py`).

### Why each permission is required

- **`Microsoft.AlertsManagement/alerts/read`** — the connector lists fired alert
  instances via the Alerts Management API (T2, `map_azure_monitor`). Read-only; it
  cannot create, resolve, or modify alerts.
- **`Microsoft.Insights/eventtypes/values/read`** — the connector reads the Activity
  Log *management* (administrative) event stream (T3, `map_azure_activity_log`). This
  is the same action Azure's built-in **Monitoring Reader** uses to read the Activity
  Log. Read-only; it cannot write diagnostic settings or read metrics.
- **`Microsoft.ResourceHealth/*/read`** — the connector reads Service Health events
  (T3, `map_service_health`) via `Microsoft.ResourceHealth/events`. Service/Resource
  Health exposes several **read-only** sub-resources (`events`, `availabilityStatuses`);
  the provider-scoped **read** wildcard is the least-privilege way to cover them
  without pinning to volatile, api-version-specific paths. It is confined to the
  single `Microsoft.ResourceHealth` provider and to `…/read` only — no write/delete.
  Security teams that prefer to pin it may substitute the explicit variant below.

#### Optional: pin ResourceHealth to explicit read actions

If your review requires no wildcards at all, replace `Microsoft.ResourceHealth/*/read`
in the role JSON with the connector's exact read (plus the sibling status read):

```json
"Actions": [
  "Microsoft.AlertsManagement/alerts/read",
  "Microsoft.Insights/eventtypes/values/read",
  "Microsoft.ResourceHealth/events/read",
  "Microsoft.ResourceHealth/availabilityStatuses/read"
]
```

Both variants are read-only and functionally equivalent for V1; the connector only
calls `Microsoft.ResourceHealth/events`.

## Permissions intentionally excluded (scope defence)

The role deliberately includes **none** of the following. Their absence is part of
the security contract:

- ❌ Built-in **Owner / Contributor / Reader / User Access Administrator** (this is a
  purpose-built custom role, not a broad built-in).
- ❌ Any **write / create / modify / delete / action** permission (`…/write`,
  `…/delete`, `…/action`).
- ❌ **Azure Monitor metrics** (`Microsoft.Insights/metrics/read`, `.../metricDefinitions/read`).
- ❌ **Log Analytics** workspaces / KQL query (`Microsoft.OperationalInsights/*`).
- ❌ **Diagnostic settings / diagnostic logs** (`Microsoft.Insights/diagnosticSettings/*`).
- ❌ **Microsoft Defender for Cloud** (`Microsoft.Security/*`).
- ❌ **Microsoft Sentinel** (`Microsoft.SecurityInsights/*`).
- ❌ Any `DataActions` — the role touches control-plane reads only.

This mirrors the connector's own scope defence: V1 ingests Alerts + Administrative
Activity Log + Service Health **only**.

---

## Deployment

### Prerequisites

- An Entra ID **service principal** (app registration) for AgentIQ. The connector
  stores its `client_id` / `client_secret` / `tenant_id` in the AgentIQ per-org vault
  (T1) — **never** in Azure config or this artifact.
- Permission to **create a custom role** and **assign roles** at the target scope
  (Owner or User Access Administrator on the subscription, or the equivalent
  delegated authorization for Lighthouse).
- The target **subscription id(s)** — the pinned set AgentIQ will read.
- Required Azure resource providers registered on the subscription:
  `Microsoft.AlertsManagement`, `Microsoft.Insights`, `Microsoft.ResourceHealth`.

### Step 1 — Create the custom role

Edit [`azure_event_connector_role.json`](azure_event_connector_role.json) and set
`AssignableScopes` to the target subscription id(s), then:

```bash
az role definition create --role-definition azure_event_connector_role.json
```

The **same role definition** applies to both clouds — no separate Government role is
needed. For **Azure US Government**, target the Gov cloud first:

```bash
az cloud set --name AzureUSGovernment
az login
az role definition create --role-definition azure_event_connector_role.json
```

### Step 2 — Assign the role to the AgentIQ service principal

Assign the custom role to the AgentIQ service principal at **subscription** scope
(the connector reads per subscription):

```bash
az role assignment create \
  --assignee "<agentiq-service-principal-object-id>" \
  --role "AgentIQ Azure Event Connector (Reader)" \
  --scope "/subscriptions/<subscription-id>"
```

### Supported assignment scopes

- **Recommended:** the **subscription** scope (one assignment per pinned
  subscription). One Azure subscription = one AgentIQ Integration Hub system.
- Management-group scope is supported by Azure but **not recommended** — it would
  grant reach beyond the explicitly pinned, Owner-approved subscription set, which
  the connector deliberately never auto-expands.

---

## Lighthouse deployment guidance

Azure Lighthouse is the primary MSP pattern: SMX's tenant is **delegated** access to
customer subscriptions, so one service principal in SMX's tenant reads many
customers' subscriptions.

- Include this custom role (by its role-definition id) in the **authorization** of
  the Lighthouse delegation offer (the `Microsoft.ManagedServices/registrationDefinitions`
  `authorizations` array), assigned to the AgentIQ service principal / group in the
  **managing (SMX) tenant**.
- The customer accepts the delegation once; no per-customer trust relationship is
  renegotiated.
- **The connected set is still pinned and Owner-approved in AgentIQ.** A newly
  delegated subscription does **not** auto-ingest — it must be explicitly added to
  the connector's pinned subscription set (MSP-B2 AC7). RBAC delegation and AgentIQ
  activation are two separate gates.

## Direct-subscription deployment guidance

For a single-tenant / direct deployment, use the same role definition:

1. Create the role in the customer subscription (Step 1).
2. Register a service principal in the **customer** tenant and assign the role at
   subscription scope (Step 2).
3. Store the service principal in AgentIQ's vault and pin the subscription.

No separate role definition is required — Lighthouse and direct assignment share the
one role.

---

## Security considerations

- **Read-only, three actions, one provider wildcard (read-scoped).** No write,
  delete, or management rights; no data-plane actions.
- **Secrets never in this artifact.** The service principal credential lives only in
  AgentIQ's Fernet-encrypted vault; this role governs *authorization*, not secrets.
- **Outbound-only.** The role enables outbound reads; it grants nothing inbound and
  needs no public ingress.
- **Revocable and observable.** Removing the role assignment (or the Lighthouse
  delegation) fully cuts the connector's access to that subscription; the connector
  reports a per-subscription authorization failure loudly rather than silently
  hiding events.
- **No scope creep.** Adding a subscription to AgentIQ requires both a role
  assignment/delegation **and** an explicit pin in the connector — neither happens
  automatically.

## Security-review checklist

- [ ] Role is **custom** and `IsCustom: true` — not a built-in.
- [ ] `Actions` contains only the three read actions above (or the pinned
      ResourceHealth variant).
- [ ] `NotActions`, `DataActions`, `NotDataActions` are empty.
- [ ] No `…/write`, `…/delete`, `…/action` anywhere.
- [ ] No Metrics / Log Analytics / Diagnostic / Defender / Sentinel actions.
- [ ] `AssignableScopes` lists only the intended subscription(s).
- [ ] Assignment is at subscription scope (not management group) unless justified.
- [ ] The AgentIQ service-principal credential is vaulted, not in config.

---

*Companion artifact: [`azure_event_connector_role.json`](azure_event_connector_role.json).
Connector implementation: `backend/discovery/ingest/azure_events.py` (+ `azure_alerts.py`,
`azure_admin_events.py`). Environment endpoints: `backend/app/azure_environments.py`.*
