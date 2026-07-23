/**
 * MSP-B13 (AT-744, T2) — MultiScopeConnectorManager.
 *
 * The provider-specific onboarding wiring for the AWS and Azure Event connectors,
 * layered on the shared, presentation-only `MultiScopeConnectorCard` (T1). This is
 * the ONE place the card meets the Cloud Connector Onboarding backend
 * (`routes_cloud_connectors.py`, T3): it loads the pinned scopes + candidates,
 * maps backend shapes onto the card's props, and wires every write
 * (create / test / pin / unpin / pin-candidate) to the vault-backed endpoints.
 *
 * Both providers share this manager — the only provider difference is the field
 * config (`multiScopeConnectors.ts`: AWS partition + role-ARN scope add; Azure
 * environment + mode + candidate-list pinning) and the scope-id key. AWS onboards
 * an account by role ARN / direct keys (T2-AC1); Azure authenticates a service
 * principal, selects an environment, and pins discovered subscriptions (T2-AC2/3/4).
 *
 * RBAC: writes are Owner-only server-side; the card's write controls are disabled
 * for non-Owners (mirrors StaticCredentialManager's real-role gating).
 */
import React, { useCallback, useEffect, useState } from 'react';
import MultiScopeConnectorCard from './MultiScopeConnectorCard';
import { multiScopeConnectorConfig } from './multiScopeConnectors';
import { toScopeHealthStatus } from './scopeHealthVocabulary';
import { Connector } from '../../types/connector';
import { ConnectedScope, SecurityArtifact, TestConnectionResult } from '../../types/multiScopeConnector';
import {
  CloudScopeView,
  createCloudConnection,
  downloadSecurityArtifact,
  fetchCloudScopes,
  fetchSecurityArtifacts,
  pinCloudScope,
  testCloudConnection,
  unpinCloudScope,
} from '../../services/cloudConnectorApi';
import { ApiError } from '../../lib/apiClient';
import { useAuthOptional } from '../../context/AuthContext';
import { useToast } from '../common/Toast';
import { useDataCache } from '../../lib/dataCache';
import { cacheKeys } from '../../lib/cacheKeys';

/** Map a backend ScopeView onto the card's ConnectedScope shape. */
function toConnectedScope(view: CloudScopeView): ConnectedScope {
  const parts: string[] = [];
  if (typeof view.event_volume_last_run === 'number') {
    parts.push(`${view.event_volume_last_run} events last run`);
  }
  if (view.role_arn) parts.push('role assumption');
  return {
    id: view.scope_id,
    identifier: view.scope_id,
    label: view.label ?? undefined,
    regions: view.regions && view.regions.length ? view.regions : undefined,
    health: {
      // The shared run-health vocabulary (T5-AC1): the backend scope status word
      // maps 1:1 to the card's status set, unknown values degrade to 'unknown'.
      status: toScopeHealthStatus(view.status),
      message: parts.length ? parts.join(' · ') : undefined,
      lastChecked: view.last_checkpoint_at ?? null,
    },
  };
}

/** Comma-separated string → trimmed, non-empty list (AWS regions). */
function splitCsv(value: string | undefined): string[] {
  return (value ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Normalise an error into the {@link ApiError}-shaped `body.detail` the card reads,
 * flattening the backend's provider-probe `{reason, message}` detail object into a
 * readable string so an actionable failure (bad role ARN, wrong partition, expired
 * secret) surfaces inline rather than as a generic fallback.
 */
function asCardError(err: unknown, fallback: string): Error & { body: { detail: string } } {
  let detail = fallback;
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown } | null;
    const d = body?.detail;
    if (typeof d === 'string') detail = d;
    else if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') {
      detail = (d as { message: string }).message;
    }
  }
  return Object.assign(new Error(detail), { body: { detail } });
}

export default function MultiScopeConnectorManager({
  connector,
}: {
  connector: Connector;
}) {
  const config = multiScopeConnectorConfig(connector.id);
  const auth = useAuthOptional();
  const isOwner = auth?.user?.role === 'owner';
  const toast = useToast();
  const cache = useDataCache();

  const isAzure = connector.id === 'azure_events';
  const scopeIdKey = isAzure ? 'subscription_id' : 'account_id';

  const [connected, setConnected] = useState<boolean>(
    connector.status === 'connected' || Boolean(connector.configured),
  );
  const [scopes, setScopes] = useState<ConnectedScope[]>([]);
  const [candidates, setCandidates] = useState<string[]>([]);
  const [loadingScopes, setLoadingScopes] = useState<boolean>(true);
  const [securityArtifacts, setSecurityArtifacts] = useState<SecurityArtifact[]>([]);

  const loadScopes = useCallback(() => {
    let alive = true;
    setLoadingScopes(true);
    fetchCloudScopes(connector.id)
      .then((resp) => {
        if (!alive) return;
        setScopes(resp.scopes.map(toConnectedScope));
        setCandidates(resp.candidates ?? []);
        if (resp.scopes.length > 0) setConnected(true);
      })
      .catch(() => {
        /* read failure is non-fatal — the onboarding form still renders */
      })
      .finally(() => {
        if (alive) setLoadingScopes(false);
      });
    return () => {
      alive = false;
    };
  }, [connector.id]);

  useEffect(() => loadScopes(), [loadScopes]);

  // Load the connector's downloadable security artifacts (IAM policy / RBAC role).
  // Best-effort: a read failure just hides the section — it never blocks onboarding.
  useEffect(() => {
    let alive = true;
    fetchSecurityArtifacts(connector.id)
      .then((resp) => {
        if (alive) setSecurityArtifacts(resp.artifacts ?? []);
      })
      .catch(() => {
        if (alive) setSecurityArtifacts([]);
      });
    return () => {
      alive = false;
    };
  }, [connector.id]);

  // A cloud connection is a new "system" set — refresh the tile list + license
  // banner after any connection/scope change so the hub reflects it with no reload.
  const invalidateHub = useCallback(() => {
    cache.invalidate(cacheKeys.connectors);
    cache.invalidate(cacheKeys.license);
  }, [cache]);

  async function handleCreate(values: Record<string, string>) {
    try {
      await createCloudConnection(connector.id, values);
    } catch (err) {
      throw asCardError(err, 'Could not save the connection.');
    }
    setConnected(true);
    toast.push(`${config?.name ?? connector.name} connection saved.`, 'success');
    loadScopes();
    invalidateHub();
  }

  async function handleTest(values: Record<string, string>): Promise<TestConnectionResult> {
    const res = await testCloudConnection(connector.id, values);
    if (res.ok) {
      return {
        ok: true,
        message: res.identity
          ? `Connection validated (${res.identity}).`
          : res.message || 'Connection validated.',
      };
    }
    return { ok: false, message: res.message || 'Test connection failed.' };
  }

  async function handleAddScope(values: Record<string, string>) {
    const body: Record<string, unknown> = { ...values };
    if ('regions' in body) body.regions = splitCsv(values.regions);
    try {
      const resp = await pinCloudScope(connector.id, body);
      setScopes(resp.scopes.map(toConnectedScope));
      setCandidates(resp.candidates ?? []);
    } catch (err) {
      throw asCardError(err, `Could not add the ${config?.scopeNoun ?? 'scope'}.`);
    }
    toast.push(`${config?.scopeNoun ?? 'Scope'} pinned.`, 'success');
    invalidateHub();
  }

  async function handlePinCandidate(candidateId: string) {
    try {
      const resp = await pinCloudScope(connector.id, { [scopeIdKey]: candidateId });
      setScopes(resp.scopes.map(toConnectedScope));
      setCandidates(resp.candidates ?? []);
      toast.push(`${config?.scopeNoun ?? 'Scope'} pinned.`, 'success');
      invalidateHub();
    } catch (err) {
      toast.push(asCardError(err, 'Could not pin the subscription.').message, 'error');
    }
  }

  async function handleRemoveScope(scopeId: string) {
    try {
      await unpinCloudScope(connector.id, scopeId);
      setScopes((prev) => prev.filter((s) => s.id !== scopeId));
      toast.push(`${config?.scopeNoun ?? 'Scope'} unpinned.`, 'success');
      invalidateHub();
    } catch (err) {
      toast.push(asCardError(err, 'Could not unpin the scope.').message, 'error');
    }
  }

  async function handleDownloadArtifact(artifactId: string) {
    const artifact = securityArtifacts.find((a) => a.id === artifactId);
    try {
      await downloadSecurityArtifact(connector.id, artifactId, artifact?.filename);
    } catch (err) {
      toast.push(
        asCardError(err, 'Could not download the security artifact.').message,
        'error',
      );
    }
  }

  // Not a multi-scope connector (shouldn't happen — the panel gates on id) → render
  // nothing rather than a broken card.
  if (!config) return null;

  return (
    <MultiScopeConnectorCard
      config={config}
      connected={connected}
      scopes={scopes}
      candidates={candidates}
      loadingScopes={loadingScopes}
      canManage={isOwner}
      manageDisabledReason={
        isOwner ? undefined : 'Onboarding a cloud connector requires an owner role.'
      }
      onCreateConnection={handleCreate}
      onTestConnection={handleTest}
      onAddScope={handleAddScope}
      onPinCandidate={isAzure ? handlePinCandidate : undefined}
      onRemoveScope={handleRemoveScope}
      securityArtifacts={securityArtifacts}
      onDownloadArtifact={handleDownloadArtifact}
    />
  );
}
