# Plan: Auto-detect & Select Salesforce Clouds/Packs after OAuth Connect

> Status: **Planned, not yet implemented.** Captured for later execution.
> Sibling of `docs/JIRA_PROJECT_PICKER_PLAN.md` — same shape (inspect the connected
> org after OAuth, let the user choose, store per-workspace, drive ingestion).
> Branch context: follows the CS-02 OAuth / DB-sourced live-ingest work.

## Motivation / request

> "Can we select available Clouds/Packs from the authenticated Salesforce?"

Today there is a **manual** Salesforce cloud declaration — the user blindly checks
which clouds their workspace uses. We want to **auto-detect** which clouds/managed
packages actually exist in the authenticated org and present those for selection,
then use the choice to drive the discovery **pack**.

## Current state

- Manual declaration endpoint: `backend/app/routes_salesforce_products.py`
  (`GET`/`PATCH /api/connectors/salesforce/products`), products stored on the
  connector record as `salesforce.products` via `org_connector_set`.
  Product IDs: `salesforce_sc`, `salesforce_ncino`, `salesforce_pss`,
  `salesforce_fsc`, `salesforce_rc`, `salesforce_hc`.
- Frontend picker: `frontend/src/components/integrations/SalesforceProductPicker.tsx`.
- Discovery packs: `backend/discovery/packs/pack_config.py`. Salesforce-relevant
  packs are `service_cloud` (default), `ncino`, `strs_benefits`.
- `SalesforceClient` (`backend/discovery/ingest/salesforce.py`) already has
  `soql()` and `tooling_soql()` + an authenticated session.

## Feasibility

**Yes.** After Connect we hold the org's instance URL + Bearer token (per-run
context, DB-sourced). Two inspection methods:
- **Describe Global** — `GET /services/data/vXX/sobjects/` lists every sObject;
  scan namespace prefixes to reveal installed clouds. Needs only read access →
  robust default.
- **Installed packages (optional)** — Tooling API
  `SELECT SubscriberPackage.Name, SubscriberPackage.NamespacePrefix FROM InstalledSubscriberPackage`
  — more authoritative but needs higher setup perms (may 403 on locked-down orgs).

### Cloud → detection signal → pack
| Cloud (product ID) | Detection signal (sObject namespace/object) | Discovery pack |
|---|---|---|
| Service Cloud (`salesforce_sc`) | `Case` (baseline) | `service_cloud` |
| nCino (`salesforce_ncino`) | `LLC_BI__*` (e.g. `LLC_BI__Loan__c`) | `ncino` |
| Public Sector / Benefits (`salesforce_pss`) | `IndividualApplication`, `BenefitAssignment` | `strs_benefits` |
| Financial Services Cloud (`salesforce_fsc`) | `FinServ__*` | *(no pack yet → `service_cloud`)* |
| Health Cloud (`salesforce_hc`) | `HealthCloudGA*` | *(no pack yet)* |
| Revenue Cloud / CPQ (`salesforce_rc`) | `SBQQ__*` | *(no pack yet)* |

---

## Implementation plan

### 1. Backend — detect available clouds (new endpoint)
`GET /api/connectors/salesforce/available-products` (analyst+, org-scoped),
parallel to the planned Jira projects endpoint:
- Read the org's SF instance + Bearer token from the DB (vault + captured URL).
- Add `SalesforceClient.describe_global()` (GET `/sobjects/`) and scan returned
  sObject names for the namespace signals above; optionally enrich with the
  Tooling installed-packages query.
- Return `[{ productId, label, available, packId, signal }]`.

### 2. Backend — persist the selection
Reuse the existing `PATCH /api/connectors/salesforce/products` (already stores
`salesforce.products` on the connector record). Optionally validate the selection
against detected availability. No new storage.

### 3. Wire selected cloud → discovery pack
- Add a `product_id → pack_id` map (the table above) in `pack_config.py` or a
  small mapper module.
- Stack Builder launch/compute already accepts `pack_id`; drive it from the
  selected cloud (augments/overrides the frontend's industry-hint `resolvePackId`).
  For a single run, the selected cloud's pack becomes the run's `pack_id`.

### 4. Frontend — enhance the existing picker
Upgrade `SalesforceProductPicker.tsx`:
- On open, `GET /available-products`; **auto-preselect detected clouds**, grey
  out / label undetected ones ("not found in your org"), show a "Detected ✓" badge.
- Keep the existing PATCH-save. Show which pack each cloud maps to so the user
  sees the discovery impact.

### 5. UX flow
Connect Salesforce (OAuth) → tile Connected → picker opens with clouds detected
in the org pre-checked → user confirms/adjusts → saved per workspace → the
selected cloud drives the discovery pack.

---

## Caveats
- **Best-effort detection.** Describe-global (read-only) is the reliable default;
  the Tooling installed-packages query is richer but may 403 — fall back to
  describe-global / manual declaration.
- **Clouds without a pack yet** (FSC, Health, Revenue) — detect and show them, but
  they currently fall back to `service_cloud`. Surface this; adding dedicated
  packs is separate work.
- **Multiple clouds / multi-pack runs** — `pack_config.py` reserves multi-pack but
  the run pipeline is single-pack today. Either pick one cloud→pack per run, or
  queue a run per selected pack (future multi-pack work).
- Multi-tenant: detection uses the org's own OAuth token from the DB; results are
  per-org.

## Effort / sequencing
- Backend (`describe_global` helper + detect endpoint + cloud→pack map):
  ~0.5–1 day; testable with mocked describe responses (`httpx.MockTransport` or a
  stubbed `SalesforceClient`).
- Frontend (enhance existing picker): ~0.5 day.
- Tests: detect-endpoint contract test with a mocked describe payload; cloud→pack
  mapping unit test.

Recommended order: backend (detect + cloud→pack wiring, verify it drives the run
pack) → frontend picker enhancement.

## Key files
- `backend/app/routes_salesforce_products.py` — existing GET/PATCH products endpoint (extend).
- `frontend/src/components/integrations/SalesforceProductPicker.tsx` — frontend picker (enhance).
- `backend/discovery/ingest/salesforce.py` — `SalesforceClient` (add `describe_global`).
- `backend/discovery/packs/pack_config.py` — pack registry + new cloud→pack map.
- `backend/app/live_ingest_credentials.py` — per-run SF credentials (DB-sourced) the detect endpoint reuses.
- `backend/app/routes_stack_builder_launch.py` — where `pack_id` enters a run.
