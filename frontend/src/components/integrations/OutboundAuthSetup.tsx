/**
 * R18-A3 T5 (AT-558) — outbound setup path for the connector detail panel.
 *
 * In a NETWORK_PROFILE=no_public_inbound deployment the browser
 * authorization-code Connect button is hidden (ConnectorTile). This component is
 * the OUTBOUND path the customer is routed to instead — it renders the
 * outbound-only setup for whichever mode the connector supports:
 *
 *   jwt_bearer          → Salesforce cert private-key form (JwtBearerCredentialModal)
 *   client_credentials  → single Owner action, acquires a service-identity token
 *                         outbound (no callback)
 *   static              → handled separately by StaticCredentialManager (Jira/
 *                         ServiceNow/DBs), so it is NOT duplicated here.
 *
 * Only shown when the deployment is no_public_inbound AND the connector has an
 * outbound-only mode other than plain `static`. Owner-gated for the write
 * actions, mirroring StaticCredentialManager.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { CheckCircle2, Lock, ShieldCheck } from 'lucide-react';
import { Connector, OutboundSetupRequest } from '../../types/connector';
import {
  ConnectorCredentialStatus,
  connectClientCredentials,
  fetchJwtBearerCredentialStatus,
} from '../../services/staticApi';
import { ApiError } from '../../lib/apiClient';
import { useAuthOptional } from '../../context/AuthContext';
import { useNetworkProfileOptional } from '../../context/NetworkProfileContext';
import { useToast } from '../common/Toast';
import JwtBearerCredentialModal from './JwtBearerCredentialModal';
import { useDataCache } from '../../lib/dataCache';
import { cacheKeys } from '../../lib/cacheKeys';

function formatUpdated(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

export default function OutboundAuthSetup({
  connector,
  outboundSetupRequest = null,
}: {
  connector: Connector;
  outboundSetupRequest?: OutboundSetupRequest | null;
}) {
  const auth = useAuthOptional();
  const isOwner = auth?.user?.role === 'owner';
  const toast = useToast();
  const cache = useDataCache();
  const { noPublicInbound, capabilityFor } = useNetworkProfileOptional();
  const capability = capabilityFor(connector.id);

  const outboundModes = capability?.outbound_only_modes ?? [];
  const supportsJwt = outboundModes.includes('jwt_bearer');
  const supportsClientCreds = outboundModes.includes('client_credentials');

  const [jwtStatus, setJwtStatus] = useState<ConnectorCredentialStatus | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  const loadJwtStatus = useCallback(() => {
    if (!supportsJwt) return () => {};
    let alive = true;
    fetchJwtBearerCredentialStatus(connector.id)
      .then((s) => {
        if (alive) setJwtStatus(s);
      })
      .catch(() => {
        if (alive) setJwtStatus(null);
      });
    return () => {
      alive = false;
    };
  }, [connector.id, supportsJwt]);

  useEffect(() => {
    return loadJwtStatus();
  }, [loadJwtStatus]);

  // The tile's "Set up outbound access" button is the single write entry point
  // (the parent bumps outboundSetupRequest.nonce). Owner-gated + guarded by a
  // consumed-nonce ref so it fires once per click and never on an unrelated
  // connector's mount. jwt_bearer opens the credential form (modal);
  // client_credentials is a formless outbound connect, triggered directly.
  const consumedNonce = useRef(0);
  useEffect(() => {
    if (!outboundSetupRequest) return;
    if (outboundSetupRequest.nonce === consumedNonce.current) return;
    consumedNonce.current = outboundSetupRequest.nonce;
    if (outboundSetupRequest.connectorId !== connector.id || !isOwner) return;
    if (supportsJwt) setModalOpen(true);
    else if (supportsClientCreds) void handleClientCredsConnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outboundSetupRequest, connector.id, isOwner, supportsJwt, supportsClientCreds]);

  // After an outbound credential change (key save/remove, or a client-credentials
  // connect), refresh this card's own status AND invalidate the connector list +
  // network profile so the tile state and auth-capability gating (now
  // cache-backed) reflect the change everywhere, with no page reload.
  function onOutboundChanged() {
    loadJwtStatus();
    cache.invalidate(cacheKeys.connectors);
    cache.invalidate(cacheKeys.networkProfile);
  }

  // Only relevant in no-public-inbound; and only for outbound modes that need a
  // dedicated setup UI here (static is handled by StaticCredentialManager).
  if (!noPublicInbound) return null;
  if (!supportsJwt && !supportsClientCreds) return null;

  async function handleClientCredsConnect() {
    try {
      await connectClientCredentials(connector.id);
      toast.push(`${connector.name} connected via client-credentials.`, 'success');
      onOutboundChanged();
    } catch (err) {
      const detail =
        err instanceof ApiError && typeof (err.body as any)?.detail === 'string'
          ? (err.body as any).detail
          : 'Could not connect via client-credentials.';
      toast.push(detail, 'error');
    }
  }

  const jwtConfigured = jwtStatus?.configured ?? false;
  const jwtUpdated = formatUpdated(jwtStatus?.updated_at ?? null);

  return (
    <div className="mt-4 rounded-lg border border-accent/25 bg-accent/5 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium text-text">
          <ShieldCheck size={14} className="shrink-0 text-accent" />
          Outbound-only access
        </div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
          No public inbound
        </div>
      </div>
      <p className="mb-3 text-[11px] leading-relaxed text-muted">
        This deployment exposes no public inbound HTTPS, so the browser sign-in
        flow cannot complete. Connect {connector.name} with an outbound-only
        method instead.
      </p>

      {/* JWT bearer (Salesforce) */}
      {supportsJwt && (
        <div className="mb-3">
          <div className="flex items-center gap-2 rounded-md border border-border bg-bg/20 px-3 py-2 text-xs">
            {jwtConfigured ? (
              <>
                <CheckCircle2 size={14} className="shrink-0 text-emerald-400" />
                <span className="min-w-0 flex-1 break-words text-text">
                  JWT bearer configured
                  {jwtUpdated ? ` · updated ${jwtUpdated}` : ''}
                  {jwtStatus?.base_url ? (
                    <span className="block truncate text-muted">{jwtStatus.base_url}</span>
                  ) : null}
                </span>
              </>
            ) : (
              <>
                <Lock size={14} className="shrink-0 text-muted" />
                <span className="min-w-0 flex-1 text-muted">JWT bearer not configured yet</span>
              </>
            )}
          </div>
          {/* Read-only: writes happen in the modal opened from the tile's
              "Set up outbound access" button. */}
          <p className="mt-2 text-[11px] leading-relaxed text-muted">
            {isOwner
              ? 'Use "Set up outbound access" on the connector card to set up, update, or remove the key.'
              : 'Only workspace Owners can set up outbound access.'}
          </p>
        </div>
      )}

      {/* client-credentials (Microsoft Graph, ServiceNow) — formless connect
          triggered from the tile's "Set up outbound access" button. */}
      {supportsClientCreds && (
        <div>
          <p className="text-[11px] leading-relaxed text-muted">
            Service-identity (client-credentials) — connects under the deployment's
            admin-consented app registration, outbound-only.
            {isOwner
              ? ' Use "Set up outbound access" on the connector card to connect.'
              : ' Only workspace Owners can set up outbound access.'}
          </p>
        </div>
      )}

      {supportsJwt && (
        <JwtBearerCredentialModal
          open={modalOpen}
          connector={connector}
          configured={jwtConfigured}
          existingBaseUrl={jwtStatus?.base_url ?? null}
          onClose={() => setModalOpen(false)}
          onSuccess={onOutboundChanged}
        />
      )}
    </div>
  );
}
