/**
 * MSP-B13 (AT-744, T2) — typed frontend wrappers for the Cloud Connector
 * Onboarding routes added by T3 (AT-745, `routes_cloud_connectors.py`).
 *
 * One connection, many scopes (AWS accounts / Azure subscriptions). Secret fields
 * are write-only — sent once, never read back (no endpoint returns them). RBAC is
 * enforced server-side (Owner creates/tests/pins/unpins; Viewer+ reads).
 *
 * Shapes mirror the backend Pydantic models exactly (see the API contract entry
 * "Cloud Connector Onboarding — AWS & Azure Events").
 */
import { apiGet, apiPost, apiDelete } from '../lib/apiClient';

/** Non-secret status of a cloud connection (never carries a credential). */
export interface CloudConnectionStatus {
  connector_id: string;
  provider: string;
  configured: boolean;
  status: string;
  partition?: string | null; // AWS
  environment?: string | null; // Azure
  mode?: string | null; // Azure (lighthouse / direct)
  scope_count: number;
  updated_at?: string | null;
}

/** Outcome of a test-connection probe (HTTP 200 with the verdict in the body). */
export interface CloudTestConnectionResult {
  connector_id: string;
  provider: string;
  ok: boolean;
  reason?: string | null;
  message: string;
  identity?: string | null;
}

/** One pinned scope as the backend reports it. */
export interface CloudScopeView {
  scope_id: string;
  kind: string;
  label?: string | null;
  status: string; // pending | ok | auth_failed | partial | failed
  pinned_at?: string | null;
  // AWS
  role_arn?: string | null;
  external_id_set?: boolean;
  regions?: string[];
  partition?: string | null;
  // Azure
  environment?: string | null;
  // populated once a run polls the scope
  last_checkpoint_at?: string | null;
  event_volume_last_run?: number | null;
}

/** The scope panel payload: pinned scopes + candidates pending Owner approval. */
export interface CloudScopesResponse {
  connector_id: string;
  provider: string;
  scopes: CloudScopeView[];
  candidates: string[];
}

/** Per-scope health — shares the run-health vocabulary. */
export interface CloudScopeHealthResponse {
  connector_id: string;
  scope_id: string;
  status: string;
  healthy: boolean;
  message?: string | null;
  last_checkpoint_at?: string | null;
  event_volume_last_run?: number | null;
  surfaces_ok?: string[];
  surfaces_failed?: Record<string, string>;
}

/** POST /api/connectors/{id} — Owner: create/rotate the connection (vault write). */
export function createCloudConnection(
  connectorId: string,
  body: Record<string, unknown>,
): Promise<CloudConnectionStatus> {
  return apiPost<CloudConnectionStatus>(`/api/connectors/${connectorId}`, body);
}

/** POST /api/connectors/{id}/test — Owner: validate auth+reachability before save. */
export function testCloudConnection(
  connectorId: string,
  body: Record<string, unknown>,
): Promise<CloudTestConnectionResult> {
  return apiPost<CloudTestConnectionResult>(`/api/connectors/${connectorId}/test`, body);
}

/** GET /api/connectors/{id}/scopes — Viewer+: pinned scopes + candidates. */
export function fetchCloudScopes(connectorId: string): Promise<CloudScopesResponse> {
  return apiGet<CloudScopesResponse>(`/api/connectors/${connectorId}/scopes`);
}

/** POST /api/connectors/{id}/scopes — Owner: pin (activate forward-only) a scope. */
export function pinCloudScope(
  connectorId: string,
  body: Record<string, unknown>,
): Promise<CloudScopesResponse> {
  return apiPost<CloudScopesResponse>(`/api/connectors/${connectorId}/scopes`, body);
}

/** DELETE /api/connectors/{id}/scopes/{scopeId} — Owner: unpin (idempotent). */
export function unpinCloudScope(connectorId: string, scopeId: string): Promise<void> {
  return apiDelete<void>(
    `/api/connectors/${connectorId}/scopes/${encodeURIComponent(scopeId)}`,
  );
}

/** GET /api/connectors/{id}/scopes/{scopeId}/health — Viewer+: per-scope health. */
export function fetchCloudScopeHealth(
  connectorId: string,
  scopeId: string,
): Promise<CloudScopeHealthResponse> {
  return apiGet<CloudScopeHealthResponse>(
    `/api/connectors/${connectorId}/scopes/${encodeURIComponent(scopeId)}/health`,
  );
}
