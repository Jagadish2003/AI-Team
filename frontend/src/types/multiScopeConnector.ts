/**
 * MSP-B13 (AT-743) — shared types for the multi-scope cloud connector onboarding
 * card (AWS & Azure Event Connectors).
 *
 * A "multi-scope" connector is one connection (a single set of vaulted, write-only
 * credentials) that fans out to MANY systems, each of which is a *scope*: an AWS
 * account or an Azure subscription. The card that renders this pattern
 * (`MultiScopeConnectorCard`) is data-driven by the shapes below so AWS and Azure
 * share one component — a fork would be a design defect (mirrors the MSP-B1/B2
 * shared-skeleton discipline on the frontend).
 *
 * Security posture (T1-AC2): every credential the connection form collects is
 * WRITE-ONLY. The values are POSTed to the vault and NEVER read back — no status
 * endpoint returns a secret, and the card never re-populates a secret field. This
 * mirrors the static-credential / JWT-bearer entry forms already in the hub.
 */

/**
 * Per-scope health status. Deliberately aligned with the backend connector-health
 * vocabularies (AWS `AWSAccountHealth.status`, Azure `subscription_status`):
 *   - ok           — the scope polled cleanly
 *   - partial      — the scope is reachable but some surfaces/streams failed
 *   - auth_failed  — credentials were rejected for this scope
 *   - failed       — the scope failed to poll
 *   - pending      — pinned but not yet polled (first load)
 *   - unknown      — no health reported yet
 */
export type ScopeHealthStatus =
  | 'ok'
  | 'partial'
  | 'auth_failed'
  | 'failed'
  | 'pending'
  | 'unknown';

/**
 * Health detail for one scope. All numeric fields are optional so a connector
 * that reports a coarser health shape (Azure just reports a per-subscription
 * status) can omit the AWS-specific per-surface counts without inventing them.
 */
export interface ScopeHealth {
  status: ScopeHealthStatus;
  /** Human-readable one-line health message (e.g. the loud failure reason). */
  message?: string;
  /** Count of surfaces/streams that polled cleanly (AWS per-account). */
  scopesOk?: number;
  /** Count of surfaces/streams that failed (AWS per-account). */
  scopesFailed?: number;
  /** Throttle events observed for this scope in the last poll (AWS). */
  throttleEvents?: number;
  /** ISO timestamp of the last health observation, if known. */
  lastChecked?: string | null;
}

/**
 * One connected scope — an AWS account or an Azure subscription — under a single
 * connection. `identifier` is the provider id (account id / subscription id);
 * `label` is an optional friendly name.
 */
export interface ConnectedScope {
  /** Stable id used for keys and remove requests (usually the identifier). */
  id: string;
  /** Provider identifier: AWS account id or Azure subscription id. */
  identifier: string;
  /** Optional friendly label; falls back to the identifier for display. */
  label?: string;
  /** Regions pinned for this scope (AWS). Omitted for Azure. */
  regions?: string[];
  /** Per-scope health. */
  health: ScopeHealth;
}

/**
 * A downloadable partner security artifact (MSP-B13 T5 / AC3/AC4) — the minimal
 * read-only AWS IAM policy or Azure Reader RBAC role a security reviewer needs.
 * Metadata only; the file content is served by the backend download route.
 */
export interface SecurityArtifact {
  /** Stable download key referenced by the download route. */
  id: string;
  /** Human label shown on the card. */
  label: string;
  /** One-line description of what the artifact grants. */
  description: string;
  /** Suggested download filename. */
  filename: string;
  /** MIME type (e.g. application/json, text/markdown). */
  media_type: string;
}

/** Result of a Test Connection attempt (T1-AC3). */
export interface TestConnectionResult {
  ok: boolean;
  /** Message to show the user — success confirmation or the failure reason. */
  message: string;
  /** Optional count of scopes reached during the test. */
  scopesReachable?: number;
}

/** One option in a `select` form field (AWS partition, Azure environment/mode). */
export interface SelectOption {
  value: string;
  label: string;
}

/**
 * One field in a connection-creation or add-scope form. A `secret` field is
 * write-only: rendered masked, never pre-filled, cleared after a successful save
 * (T1-AC2). A field carrying `options` renders as a `<select>` (MSP-B13 T2 — AWS
 * partition / Azure environment + access mode).
 */
export interface ConnectorFormField {
  /** Payload key sent to the create/add-scope handler. */
  key: string;
  label: string;
  placeholder?: string;
  /** One-line helper shown under the field. */
  hint?: string;
  /** Write-only secret — masked input, never displayed after save. */
  secret?: boolean;
  /** Whether the field must be non-empty before submit. Defaults to true. */
  required?: boolean;
  /** Present => the field is a dropdown selection (e.g. partition/environment). */
  options?: SelectOption[];
  /** Initial/reset value — especially for a `select` (e.g. the default partition). */
  defaultValue?: string;
}

/**
 * Field/copy configuration for one multi-scope connector. This is the frontend
 * source of truth for AWS/Azure onboarding copy; the actual API wiring is passed
 * to the card as handlers (so this stays presentation-only and the card stays
 * decoupled from unbuilt endpoints).
 */
export interface MultiScopeConnectorConfig {
  /** Connector id in the catalog (`aws_events` / `azure_events`). */
  connectorId: string;
  /** Display name. */
  name: string;
  /** One-line description of the connection. */
  description: string;
  /** Singular noun for a scope, e.g. "account" / "subscription". */
  scopeNoun: string;
  /** Plural noun for scopes, e.g. "accounts" / "subscriptions". */
  scopeNounPlural: string;
  /** Fields for the connection-creation form (the vaulted credentials). */
  credentialFields: ConnectorFormField[];
  /** Fields for pinning a new scope (account/subscription). */
  scopeFields: ConnectorFormField[];
  /** One-line helper shown under the credential form. */
  credentialHint?: string;
}
