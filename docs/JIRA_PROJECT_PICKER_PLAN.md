# Plan: Jira Project Picker after OAuth Connect

> Status: **Planned, not yet implemented.** Captured for later execution.
> Branch context: follows the CS-02 OAuth / DB-sourced live-ingest work.

## Trigger / motivation

During a live Jira run the ingest logs:

```
No Scrum boards found for Jira project AIC. Sprint velocity will be empty.
Project may be Kanban or non-sprint based.
```

Root cause: the Jira project is hardcoded to a default — `os.getenv("JIRA_PROJECT_KEY", "AIC")` — in `backend/discovery/ingest/jira.py`. The connecting user has no way to choose which Jira project to ingest, so it falls back to `AIC`, which has no Scrum board.

## User request

> "When I connect to Jira, I want the Jira OAuth to ask which Space [project] to choose. Is this possible?"

(Terminology: in **Jira** this is a **project** (the `AIC` key). "Space" is the **Confluence** term. This plan covers Jira projects; the same shape applies to Confluence spaces later.)

## Feasibility

**Yes — as a post-connect step, not inside the OAuth consent.** Jira 3LO is a browser redirect to Atlassian; we cannot inject our own UI mid-redirect. But immediately after Connect succeeds we already hold the cloudId gateway base + Bearer token (per-run context, DB-sourced), so the app can list the user's projects and present a picker. The chosen project replaces the `AIC` default.

**Precedent to copy:** the Salesforce **product** picker —
`backend/app/routes_salesforce_products.py` (GET/PATCH `/api/connectors/salesforce/products`, stored on the connector record via `org_connector_set`) + `frontend/src/components/integrations/SalesforceProductPicker.tsx`. The Jira client already has `JiraClient.get_boards(project_key)` to distinguish Scrum vs Kanban.

---

## Implementation plan

### 1. Backend — list projects (new endpoint)
`GET /api/connectors/jira/projects` (analyst+, org-scoped), mirroring `routes_salesforce_products.py`:
- Read the org's Jira gateway base + Bearer token from the DB (vault token + captured `connector_instance_url:{org}:jira`).
- `GET {gateway}/rest/api/3/project/search` → `[{key, name, id}]` (paginated; follow `isLast`/`nextPage`).
- `GET {gateway}/rest/agile/1.0/board?maxResults=...` once, group by `location.projectKey` + `type` to flag **which projects have a Scrum board**.
- Return `[{ key, name, hasScrumBoard }]`.

### 2. Backend — persist the choice (new endpoint)
`PATCH /api/connectors/jira/project` with `{ projectKey }`:
- Validate against the live project list.
- Store **per-org** on the connector record via `org_connector_set` as `jira.projectKey` (same pattern as `salesforce.products`). Workspace-level fact, not per-run.

### 3. Wire the chosen project into ingestion
- `backend/app/live_ingest_credentials.py` `resolve_live_systems`: add `project_key` to the Jira entry of the per-run context →
  `{"jira": {"url": ..., "token": ..., "project_key": ...}}` (read from the connector record).
- `backend/discovery/ingest/jira.py`: add `_project_key()` helper that reads the per-run context first, then `JIRA_PROJECT_KEY` env, then `"AIC"`. Replace the ~5 `os.getenv("JIRA_PROJECT_KEY", "AIC")` call sites (≈ lines 305, 432, 573, 603, 857) with it.
- Keeps it multi-tenant safe + DB-sourced (consistent with `discovery/ingest/__init__.py` `set_live_connectors`/`get_live_connector`).

### 4. Frontend — the picker
Mirror `SalesforceProductPicker.tsx`:
- New `frontend/src/components/integrations/JiraProjectPicker.tsx` — `GET`s `/api/connectors/jira/projects`, `PATCH`es selection.
- Trigger from the Jira tile's **Configure & Sync** action (or auto-open once on first successful Jira connect).
- Tile shows the selected project (e.g. "Project: ENG") with a "Scrum board ✓ / Kanban — no velocity" hint.
- Optionally gate "Configure & Sync" / discovery for Jira until a project is selected.

### 5. UX flow
Connect Jira (OAuth) → tile shows Connected → picker lists real projects with Scrum-board flags → user picks one → saved per workspace → discovery runs use it. Re-openable to switch.

---

## Caveats
1. **Kanban reality:** picking a project only clears the warning if it has a **Scrum board**. Kanban projects have no sprints → velocity empty by definition. Surface `hasScrumBoard` so the user chooses knowingly.
2. **Multiple Jira sites:** `accessible-resources` can return >1 site (currently we take the first and log a warning). Optional extension: a **site picker** before the project picker when the token grants several sites.

## Effort / sequencing
- Backend (2 endpoints + ingest wiring): ~0.5 day. Test with `httpx.MockTransport` (as in the existing Jira gateway tests).
- Frontend (picker + tile integration): ~0.5 day, copied from the Salesforce picker.
- Tests: contract tests for both endpoints; unit test that `_project_key()` resolves context → env → default.

Recommended order: backend first (verify project selection drives the run), then the frontend picker, then optional site picker.

## Key files
- `backend/discovery/ingest/jira.py` — `JiraClient.get_boards()`, `get_sprint_velocity()` (warning source), `JIRA_PROJECT_KEY` reads.
- `backend/app/live_ingest_credentials.py` — per-run context + captured instance URL pattern.
- `backend/discovery/ingest/__init__.py` — `set_live_connectors` / `get_live_connector`.
- `backend/app/routes_salesforce_products.py` — endpoint precedent.
- `frontend/src/components/integrations/SalesforceProductPicker.tsx` — frontend precedent.
- `backend/app/routes_connector_auth.py` — OAuth callback where the Jira gateway/cloudId is captured.
