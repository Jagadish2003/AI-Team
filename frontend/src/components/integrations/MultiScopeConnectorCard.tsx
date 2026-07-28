/**
 * MSP-B13 (AT-743, T1) — MultiScopeConnectorCard.
 *
 * The Integration Hub's first multi-scope connector card: ONE connection (a
 * single set of vaulted, write-only credentials) that fans out to MANY systems,
 * each of which is a *scope* — an AWS account or an Azure subscription. AWS and
 * Azure share this one component; it is driven entirely by a
 * `MultiScopeConnectorConfig` (field/copy) plus handler callbacks, so a fork for
 * either provider would be a design defect (mirrors the MSP-B1/B2 shared-skeleton
 * discipline on the frontend).
 *
 * The card is presentation + interaction only — it never calls the API directly.
 * The parent (wired in a later B13 task) owns the actual vault write / test /
 * scope-pin calls and passes them in as `onCreateConnection` / `onTestConnection`
 * / `onAddScope` / `onRemoveScope`. This keeps the card decoupled from the cloud
 * endpoints and trivially testable.
 *
 * Acceptance criteria:
 *   T1-AC1 — supports cloud connector onboarding (create-connection form).
 *   T1-AC2 — secret fields are WRITE-ONLY: masked, never pre-filled, cleared on
 *            save, and never re-populated from any prop.
 *   T1-AC3 — Test Connection is integrated into the onboarding flow.
 *   T1-AC4 — the scope panel displays connected scopes + per-scope health.
 */
import React, { useMemo, useState } from 'react';
import {
  Cloud,
  KeyRound,
  Plug,
  PlugZap,
  Trash2,
  CheckCircle2,
  XCircle,
  ShieldCheck,
  Download,
  Lock,
  Loader2,
} from 'lucide-react';
import Button from '../common/Button';
import PasswordInput from '../auth/PasswordInput';
import {
  ConnectedScope,
  ConnectorFormField,
  MultiScopeConnectorConfig,
  ScopeHealthStatus,
  SecurityArtifact,
  TestConnectionResult,
} from '../../types/multiScopeConnector';
import { scopeHealthPresentation } from './scopeHealthVocabulary';

const INPUT_CLS =
  'w-full rounded-lg border border-border bg-bg/30 px-3 py-2 text-sm text-text ' +
  'placeholder:text-muted/60 outline-none transition-colors hover:border-accent/40 ' +
  'focus:border-accent focus:ring-2 focus:ring-accent/20';

// ── Per-scope health badge ──────────────────────────────────────────────────
// Renders the SHARED run-health vocabulary (T5-AC1) from scopeHealthVocabulary so
// the card and any run-health surface use the identical status words. The
// `data-tone` marker carries the healthy/warn/error/neutral severity so healthy
// and failed scopes are unambiguously distinguishable (T5-AC2), independent of
// colour (the icon reinforces it too).
function ScopeHealthBadge({ status }: { status: ScopeHealthStatus }) {
  const p = scopeHealthPresentation(status);
  const { Icon } = p;
  return (
    <span
      data-testid="scope-health-badge"
      data-tone={p.tone}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-none ${p.cls}`}
    >
      <Icon size={11} className="shrink-0" />
      {p.label}
    </span>
  );
}

// ── A single text/secret form field ─────────────────────────────────────────
function FormFieldInput({
  field,
  value,
  onChange,
  disabled,
  idPrefix,
}: {
  field: ConnectorFormField;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  idPrefix: string;
}) {
  const id = `${idPrefix}-${field.key}`;
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium text-text">
        {field.label}
      </label>
      {field.options ? (
        // MSP-B13 T2: a dropdown selection (AWS partition, Azure environment/mode).
        <select
          id={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          className={INPUT_CLS}
        >
          {field.options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      ) : field.secret ? (
        // Never pre-filled; autoComplete=new-password so the browser does not
        // autofill a stored value into this write-only field (T1-AC2).
        <PasswordInput
          id={id}
          value={value}
          onChange={onChange}
          placeholder={field.placeholder ?? `Enter ${field.label.toLowerCase()}`}
          autoComplete="new-password"
          disabled={disabled}
        />
      ) : (
        <input
          id={id}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          autoComplete="off"
          disabled={disabled}
          className={INPUT_CLS}
        />
      )}
      {field.hint && <p className="text-[11px] leading-relaxed text-muted">{field.hint}</p>}
    </div>
  );
}

/**
 * Build the initial/reset value map for a set of fields. A select resets to its
 * `defaultValue` (or first option) so a required dropdown always carries a value;
 * every other field (incl. write-only secrets) resets to empty (T1-AC2).
 */
function emptyValues(fields: ConnectorFormField[]): Record<string, string> {
  return fields.reduce<Record<string, string>>((acc, f) => {
    acc[f.key] = f.defaultValue ?? (f.options ? f.options[0]?.value ?? '' : '');
    return acc;
  }, {});
}

/** The keys of every field the form requires (defaults to required). */
function missingRequired(
  fields: ConnectorFormField[],
  values: Record<string, string>,
): string[] {
  return fields
    .filter((f) => f.required !== false)
    .filter((f) => !(values[f.key] ?? '').trim())
    .map((f) => f.label);
}

interface Props {
  config: MultiScopeConnectorConfig;
  /**
   * Provider-branded icon for the card header (reuses the Integration Hub's
   * shared connector-icon registry). Defaults to the generic cloud mark.
   */
  icon?: React.ReactNode;
  /** Whether a connection (credential) already exists for this org. */
  connected: boolean;
  /**
   * True while a create/connect submission is in flight, so the header can show
   * a transient "Connecting…" state instead of a premature "Connected" badge.
   * Optional — the card also tracks its own in-flight save; this lets the parent
   * reflect connecting state it drives.
   */
  connecting?: boolean;
  /** Optional non-secret summary of the stored connection (e.g. masked identity). */
  connectionSummary?: string | null;
  /** The scopes (accounts/subscriptions) pinned under this connection. */
  scopes?: ConnectedScope[];
  /**
   * Discovered-but-unpinned scopes (Azure subscriptions) surfaced for Owner
   * approval (T2-AC3). They are NEVER active until explicitly pinned (T2-AC4).
   */
  candidates?: string[];
  /** True while scopes are being (re)loaded. */
  loadingScopes?: boolean;
  /**
   * Whether the current user may manage the connector (owner/analyst). When
   * false the write controls are disabled with `manageDisabledReason` as tooltip.
   */
  canManage?: boolean;
  manageDisabledReason?: string;
  /** Create/replace the connection credentials (vault write). */
  onCreateConnection: (values: Record<string, string>) => Promise<void>;
  /** Test the connection with the entered (or stored) credentials. */
  onTestConnection?: (values: Record<string, string>) => Promise<TestConnectionResult>;
  /** Pin a new scope (account/subscription). */
  onAddScope?: (values: Record<string, string>) => Promise<void>;
  /** Pin a discovered candidate scope by its identifier (T2-AC4). */
  onPinCandidate?: (candidateId: string) => Promise<void>;
  /** Unpin a scope by id. */
  onRemoveScope?: (scopeId: string) => Promise<void>;
  /**
   * Downloadable partner security artifacts — the minimal read-only IAM policy
   * (AWS) / Reader RBAC role (Azure). Shown in the "Security & compliance" section
   * so a reviewer can grab them in the flow (T5-AC3/AC4). Read-only, so available
   * regardless of `canManage`.
   */
  securityArtifacts?: SecurityArtifact[];
  /** Download one security artifact by id (the parent triggers the save). */
  onDownloadArtifact?: (artifactId: string) => Promise<void> | void;
}

export default function MultiScopeConnectorCard({
  config,
  icon,
  connected,
  connecting = false,
  connectionSummary = null,
  scopes = [],
  candidates = [],
  loadingScopes = false,
  canManage = true,
  manageDisabledReason,
  onCreateConnection,
  onTestConnection,
  onAddScope,
  onPinCandidate,
  onRemoveScope,
  securityArtifacts = [],
  onDownloadArtifact,
}: Props) {
  // ── Connection form state ─────────────────────────────────────────────────
  const [credValues, setCredValues] = useState<Record<string, string>>(() =>
    emptyValues(config.credentialFields),
  );
  const [savingCred, setSavingCred] = useState(false);
  const [credError, setCredError] = useState<string | null>(null);

  // Transient connecting state: a save is in flight here, OR the parent signals
  // one. Drives the header's "Connecting…" badge and blocks a premature green.
  const isConnecting = !connected && (connecting || savingCred);

  // ── Test-connection state (T1-AC3) ────────────────────────────────────────
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);

  // ── Add-scope form state ──────────────────────────────────────────────────
  const [scopeValues, setScopeValues] = useState<Record<string, string>>(() =>
    emptyValues(config.scopeFields),
  );
  const [addingScope, setAddingScope] = useState(false);
  const [scopeError, setScopeError] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [pinningId, setPinningId] = useState<string | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  const disabledTitle = !canManage ? manageDisabledReason : undefined;

  const credComplete = useMemo(
    () => missingRequired(config.credentialFields, credValues).length === 0,
    [config.credentialFields, credValues],
  );
  const scopeComplete = useMemo(
    () => missingRequired(config.scopeFields, scopeValues).length === 0,
    [config.scopeFields, scopeValues],
  );

  function setCred(key: string, v: string) {
    setCredValues((prev) => ({ ...prev, [key]: v }));
    // A credential edit invalidates the last test result.
    setTestResult(null);
  }

  function setScope(key: string, v: string) {
    setScopeValues((prev) => ({ ...prev, [key]: v }));
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!canManage) return;
    setCredError(null);
    const missing = missingRequired(config.credentialFields, credValues);
    if (missing.length > 0) {
      setCredError(`Enter the ${missing.join(', ')}.`);
      return;
    }
    setSavingCred(true);
    try {
      await onCreateConnection(trimValues(credValues));
      // T1-AC2: secrets are write-only — clear the whole form on success so no
      // secret lingers in state or the DOM and nothing is ever read back.
      setCredValues(emptyValues(config.credentialFields));
      setTestResult(null);
    } catch (err) {
      setCredError(errorDetail(err, 'Could not save the connection.'));
    } finally {
      setSavingCred(false);
    }
  }

  async function handleTest() {
    if (!onTestConnection || !canManage) return;
    setTestResult(null);
    setCredError(null);
    setTesting(true);
    try {
      const result = await onTestConnection(trimValues(credValues));
      setTestResult(result);
    } catch (err) {
      setTestResult({ ok: false, message: errorDetail(err, 'Test connection failed.') });
    } finally {
      setTesting(false);
    }
  }

  async function handleAddScope(e: React.FormEvent) {
    e.preventDefault();
    if (!onAddScope || !canManage) return;
    setScopeError(null);
    const missing = missingRequired(config.scopeFields, scopeValues);
    if (missing.length > 0) {
      setScopeError(`Enter the ${missing.join(', ')}.`);
      return;
    }
    setAddingScope(true);
    try {
      await onAddScope(trimValues(scopeValues));
      setScopeValues(emptyValues(config.scopeFields));
    } catch (err) {
      setScopeError(errorDetail(err, `Could not add the ${config.scopeNoun}.`));
    } finally {
      setAddingScope(false);
    }
  }

  async function handleRemoveScope(scopeId: string) {
    if (!onRemoveScope || !canManage) return;
    setRemovingId(scopeId);
    try {
      await onRemoveScope(scopeId);
    } finally {
      setRemovingId(null);
    }
  }

  async function handlePinCandidate(candidateId: string) {
    if (!onPinCandidate || !canManage) return;
    setPinningId(candidateId);
    try {
      await onPinCandidate(candidateId);
    } finally {
      setPinningId(null);
    }
  }

  async function handleDownloadArtifact(artifactId: string) {
    // Downloads are read-only partner docs — available to any role (not gated by
    // canManage), so a security reviewer can grab them without edit rights.
    if (!onDownloadArtifact) return;
    setDownloadingId(artifactId);
    try {
      await onDownloadArtifact(artifactId);
    } finally {
      setDownloadingId(null);
    }
  }

  return (
    <div className="rounded-xl border border-border bg-panel p-5 shadow-sm">
      {/* Header. The status badge is driven ONLY by the backend connection state
          (`connected`) with a transient "Connecting…" while a save is in flight —
          it never shows green before the backend has confirmed the connection. */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-accent/20 bg-accent/10 text-accent">
            {icon ?? <Cloud size={18} />}
          </span>
          <div className="min-w-0">
            <h3 className="flex items-center gap-2 text-base font-semibold text-text">
              {config.name}
              {connected ? (
                <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium leading-none text-emerald-300">
                  <PlugZap size={11} /> Connected
                </span>
              ) : isConnecting ? (
                <span className="inline-flex items-center gap-1 rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[11px] font-medium leading-none text-sky-300">
                  <Loader2 size={11} className="animate-spin" /> Connecting…
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 rounded-full border border-border bg-slate-500/10 px-2 py-0.5 text-[11px] font-medium leading-none text-muted">
                  <Plug size={11} /> Not connected
                </span>
              )}
            </h3>
            <p className="mt-1 text-xs leading-relaxed text-muted">{config.description}</p>
          </div>
        </div>
        {connected && (
          <div className="shrink-0 text-right text-[11px] text-muted">
            {scopes.length} {scopes.length === 1 ? config.scopeNoun : config.scopeNounPlural}
          </div>
        )}
      </div>

      {/* RBAC (MSP-B13 AT-748, T6-AC3): Analyst/Viewer see the scope panel +
          per-scope health but cannot modify the configuration. Make the
          read-only posture explicit; the write controls below stay disabled. */}
      {!canManage && (
        <div
          data-testid="cloud-connector-readonly"
          className="mt-4 flex items-center gap-2 rounded-lg border border-border bg-bg/20 px-3 py-2 text-[11px] text-muted"
        >
          <Lock size={13} className="shrink-0" />
          <span>
            Read-only — {manageDisabledReason ?? 'you can view connection health but not modify it.'}
          </span>
        </div>
      )}

      {/* ── Connection credentials (T1-AC1 / AC2) ── */}
      <section className="mt-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-text">
          <KeyRound size={14} className="shrink-0 text-accent" />
          {connected ? 'Update connection credentials' : 'Connect'}
        </div>
        {config.credentialHint && (
          <p className="mb-3 text-[11px] leading-relaxed text-muted">{config.credentialHint}</p>
        )}
        {connected && (
          <p className="mb-3 rounded-lg border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200">
            A connection is already stored{connectionSummary ? ` (${connectionSummary})` : ''}.
            Saving replaces it — stored secrets can never be shown.
          </p>
        )}

        <form onSubmit={handleCreate} noValidate className="space-y-3">
          {config.credentialFields.map((field) => (
            <FormFieldInput
              key={field.key}
              field={field}
              value={credValues[field.key] ?? ''}
              onChange={(v) => setCred(field.key, v)}
              disabled={savingCred || !canManage}
              idPrefix={`${config.connectorId}-cred`}
            />
          ))}

          {credError && (
            <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
              {credError}
            </p>
          )}

          {/* Test result banner (T1-AC3) */}
          {testResult && (
            <p
              role="status"
              className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm ${
                testResult.ok
                  ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                  : 'border-red-500/30 bg-red-500/10 text-red-300'
              }`}
            >
              {testResult.ok ? (
                <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
              ) : (
                <XCircle size={16} className="mt-0.5 shrink-0" />
              )}
              <span className="min-w-0">
                {testResult.message}
                {testResult.ok && typeof testResult.scopesReachable === 'number' && (
                  <span className="block text-emerald-300/80">
                    {testResult.scopesReachable} {config.scopeNounPlural} reachable.
                  </span>
                )}
              </span>
            </p>
          )}

          <div className="flex items-center gap-3 pt-1">
            {onTestConnection && (
              <Button
                variant="secondary"
                type="button"
                disabled={testing || savingCred || !canManage || !credComplete}
                title={
                  disabledTitle ??
                  (!credComplete ? 'Enter the credentials first.' : undefined)
                }
                onClick={handleTest}
              >
                {testing ? 'Testing…' : 'Test connection'}
              </Button>
            )}
            <Button
              variant="primary"
              type="submit"
              disabled={savingCred || !canManage || !credComplete}
              title={disabledTitle}
            >
              {savingCred
                ? 'Connecting…'
                : connected
                ? 'Replace credentials'
                : 'Save & connect'}
            </Button>
          </div>
        </form>
      </section>

      {/* ── Scope panel (T1-AC4) ── */}
      <section className="mt-6 border-t border-border pt-5">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="text-sm font-medium text-text">
            Connected {config.scopeNounPlural}
          </div>
          <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
            Each {config.scopeNoun} is a system
          </div>
        </div>

        {loadingScopes ? (
          <div className="text-xs text-muted animate-pulse">Loading {config.scopeNounPlural}…</div>
        ) : scopes.length === 0 ? (
          <p className="rounded-lg border border-border bg-bg/20 px-3 py-3 text-xs text-muted">
            {connected
              ? `No ${config.scopeNounPlural} pinned yet. Add one below to start ingesting.`
              : `Connect first, then pin the ${config.scopeNounPlural} to ingest.`}
          </p>
        ) : (
          <ul className="space-y-2" aria-label={`Connected ${config.scopeNounPlural}`}>
            {scopes.map((scope) => (
              <li
                key={scope.id}
                className="flex items-start justify-between gap-3 rounded-lg border border-border bg-bg/20 px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium text-text">
                      {scope.label || scope.identifier}
                    </span>
                    <ScopeHealthBadge status={scope.health.status} />
                  </div>
                  <div className="mt-0.5 truncate text-[11px] text-muted">{scope.identifier}</div>
                  {scope.regions && scope.regions.length > 0 && (
                    <div className="mt-0.5 truncate text-[11px] text-muted">
                      Regions: {scope.regions.join(', ')}
                    </div>
                  )}
                  {scope.health.message && (
                    <div className="mt-0.5 break-words text-[11px] text-muted">
                      {scope.health.message}
                    </div>
                  )}
                  {(typeof scope.health.scopesOk === 'number' ||
                    typeof scope.health.scopesFailed === 'number' ||
                    typeof scope.health.throttleEvents === 'number') && (
                    <div className="mt-0.5 flex flex-wrap gap-x-3 text-[11px] text-muted">
                      {typeof scope.health.scopesOk === 'number' && (
                        <span>{scope.health.scopesOk} ok</span>
                      )}
                      {typeof scope.health.scopesFailed === 'number' &&
                        scope.health.scopesFailed > 0 && (
                          <span className="text-red-300">
                            {scope.health.scopesFailed} failed
                          </span>
                        )}
                      {typeof scope.health.throttleEvents === 'number' &&
                        scope.health.throttleEvents > 0 && (
                          <span className="text-amber-200">
                            {scope.health.throttleEvents} throttled
                          </span>
                        )}
                    </div>
                  )}
                </div>
                {onRemoveScope && (
                  <button
                    type="button"
                    aria-label={`Remove ${scope.label || scope.identifier}`}
                    title={disabledTitle ?? `Remove ${config.scopeNoun}`}
                    disabled={!canManage || removingId === scope.id}
                    onClick={() => handleRemoveScope(scope.id)}
                    className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border text-muted transition-colors hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30"
                  >
                    <Trash2 size={14} />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}

        {/* Candidate scopes (T2-AC3/AC4) — discovered but NOT yet ingesting.
            Each requires an explicit Owner Pin before it activates. */}
        {onPinCandidate && candidates.length > 0 && (
          <div className="mt-4" data-testid="candidate-scopes">
            <div className="mb-2 flex items-center gap-2">
              <span className="text-xs font-medium text-text">
                Discovered {config.scopeNounPlural}
              </span>
              <span className="inline-flex items-center rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] font-medium leading-none text-sky-300">
                Pending approval
              </span>
            </div>
            <p className="mb-2 text-[11px] leading-relaxed text-muted">
              These {config.scopeNounPlural} were discovered but are NOT ingested.
              Pin one to activate it.
            </p>
            <ul className="space-y-2" aria-label={`Candidate ${config.scopeNounPlural}`}>
              {candidates.map((candidate) => (
                <li
                  key={candidate}
                  className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-sky-500/30 bg-sky-500/5 px-3 py-2"
                >
                  <span className="min-w-0 truncate text-[11px] text-text">{candidate}</span>
                  <Button
                    variant="secondary"
                    type="button"
                    disabled={!canManage || pinningId === candidate}
                    title={disabledTitle}
                    onClick={() => handlePinCandidate(candidate)}
                    ariaLabel={`Pin ${candidate}`}
                  >
                    {pinningId === candidate ? 'Pinning…' : 'Pin'}
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Add-scope form — only meaningful once a connection exists. */}
        {onAddScope && connected && (
          <form onSubmit={handleAddScope} noValidate className="mt-4 space-y-3">
            <div className="text-xs font-medium text-text">
              Add {aOrAn(config.scopeNoun)} {config.scopeNoun}
            </div>
            {config.scopeFields.map((field) => (
              <FormFieldInput
                key={field.key}
                field={field}
                value={scopeValues[field.key] ?? ''}
                onChange={(v) => setScope(field.key, v)}
                disabled={addingScope || !canManage}
                idPrefix={`${config.connectorId}-scope`}
              />
            ))}
            {scopeError && (
              <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {scopeError}
              </p>
            )}
            <Button
              variant="tertiary"
              type="submit"
              disabled={addingScope || !canManage || !scopeComplete}
              title={disabledTitle}
            >
              {addingScope ? 'Adding…' : `Add ${config.scopeNoun}`}
            </Button>
          </form>
        )}
      </section>

      {/* ── Security & compliance (T5-AC3/AC4) ── */}
      {securityArtifacts.length > 0 && (
        <section
          className="mt-6 border-t border-border pt-5"
          data-testid="security-artifacts"
        >
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-text">
            <ShieldCheck size={14} className="shrink-0 text-accent" />
            Security &amp; compliance
          </div>
          <p className="mb-3 text-[11px] leading-relaxed text-muted">
            The minimal read-only access this connector needs — hand these to your
            security reviewer.
          </p>
          <ul className="space-y-2" aria-label="Security artifacts">
            {securityArtifacts.map((artifact) => (
              <li
                key={artifact.id}
                className="flex items-start justify-between gap-3 rounded-lg border border-border bg-bg/20 px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-text">
                    {artifact.label}
                  </div>
                  <div className="mt-0.5 break-words text-[11px] text-muted">
                    {artifact.description}
                  </div>
                </div>
                {onDownloadArtifact && (
                  <button
                    type="button"
                    onClick={() => handleDownloadArtifact(artifact.id)}
                    disabled={downloadingId === artifact.id}
                    aria-label={`Download ${artifact.label}`}
                    title={`Download ${artifact.filename}`}
                    className="inline-flex h-8 shrink-0 items-center gap-1 rounded-md border border-border px-2 text-[11px] text-muted transition-colors hover:border-accent/40 hover:bg-accent/10 hover:text-accent disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  >
                    <Download size={13} className="shrink-0" />
                    {downloadingId === artifact.id ? 'Downloading…' : 'Download'}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

// ── helpers ──────────────────────────────────────────────────────────────────

function trimValues(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).map(([k, v]) => [k, (v ?? '').trim()]));
}

function aOrAn(noun: string): string {
  return /^[aeiou]/i.test(noun) ? 'an' : 'a';
}

/** Narrow an ApiError-shaped error to its `detail`, else the fallback. */
function errorDetail(err: unknown, fallback: string): string {
  const body = (err as { body?: { detail?: unknown }; status?: number } | null)?.body;
  if (body && typeof body.detail === 'string') return body.detail;
  return fallback;
}
