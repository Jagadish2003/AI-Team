import React, { useEffect, useState } from 'react';
import { Loader2, Unplug } from 'lucide-react';
import { Connector, ConnectorStatus } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { fetchTokenStatus, TokenStatus } from '../../services/staticApi';
import { useAuthOptional } from '../../context/AuthContext';
import { useNetworkProfileOptional } from '../../context/NetworkProfileContext';
import { isViewerRole } from '../../utils/roles';
import { useResource } from '../../lib/dataCache';
import { cacheKeys } from '../../lib/cacheKeys';
import { showRoadmapComingSoonLabels } from '../../config/releaseFlags';
import { isConnectorEnabled, connectorBadgeStatus } from './connectorEnablement';


export default function ConnectorTile({
  connector,
  icon,
  selected,
  onSelect,
  onPrimary,
  onReconnect,
  onDisconnect,
  onSetupOutbound,
  connectBlocked,
  connectBlockMessage,
  connecting,
}: {
  connector: Connector;
  icon: React.ReactNode;
  selected: boolean;
  onSelect: () => void;
  onPrimary: () => void;
  // R18-A3 follow-up: in a no-public-inbound deployment the outbound-only
  // connectors show a "Set up outbound access" button instead of Connect.
  // Clicking it should POP the credential/JWT setup modal directly (like the
  // right-panel "Enter credentials" flow), not just re-select the tile. The
  // parent selects the connector and bumps the auto-open request. Falls back to
  // onSelect when not supplied so the tile still works in isolation.
  onSetupOutbound?: () => void;
  // Called when the token is expired/refresh-failed and the user clicks
  // "Reconnect". Must trigger the OAuth flow again (CS-2 AC7). Falls back to
  // onPrimary when not supplied so the tile keeps working in isolation.
  onReconnect?: () => void;
  // R18-C0 P4 / AT-566: called when the user clicks the tile's Disconnect action
  // (shown only on a connected tile). The parent owns the confirmation step and
  // the disconnect request; omitting it hides the action (e.g. read-only views).
  onDisconnect?: () => void;
  // R17-D4 Addendum A / T11 (AT-506): when the org is at its licensed system
  // limit, disable the Connect action for a NEW (not-yet-connected) system and
  // show connectBlockMessage as its tooltip (AC10). Forward-only — an already
  // connected system keeps its Configure/View/Reconnect action, since
  // reconnecting is not a new connection and is never blocked.
  connectBlocked?: boolean;
  connectBlockMessage?: string;
  // True while THIS connector's OAuth round-trip is in flight (the parent owns
  // the flag). Connecting mints a one-time state nonce and then redirects the
  // browser, so the action must be a single click: the button disables and reads
  // "Connecting…" until the redirect lands — the same posture as the Stack
  // Builder "Start discovery" button while a run is launching.
  connecting?: boolean;
}) {
  const isConnected = connector.status === 'connected';
  const isConfigured = connector.configured;
  const isConnecting = Boolean(connecting);
  // MSP-B13 (AT-748): a multi-scope cloud connector (AWS/Azure Events) onboards
  // in the detail panel (credentials + scope pinning + per-scope health), NOT via
  // the tile's OAuth Connect flow. Its tile action just opens that panel — enabled
  // for every role, since Analyst/Viewer open it to VIEW health (read-only) and
  // the panel enforces Owner-only edits. So it bypasses the OAuth-only enablement
  // gate (see connectorEnablement.ts).
  const isMultiScope = Boolean(connector.multiScope);
  const isEnabled = isConnectorEnabled(connector);

  // The status pill is derived from the SAME gate as the action, not from the raw
  // catalog status — see connectorEnablement.ts for the rule.
  const badgeStatus: ConnectorStatus = connectorBadgeStatus(connector);

  // R191-R1 T5 (AT-726): a roadmap connector (SAP/D365 and any tile whose
  // ingestion does not ship yet) is never connectable. Keep the release target
  // available for metadata/tooltips, but use one clean visible label everywhere.
  //
  // The customer-facing labelling is withdrawn behind `showRoadmapComingSoonLabels`
  // (see config/releaseFlags.ts). With it off, the tile falls back to its ordinary
  // status badge and the pre-existing ENABLED_CONNECTOR_IDS product gate — which
  // already excludes every roadmap connector, so nothing becomes connectable.
  const isRoadmap = showRoadmapComingSoonLabels && connector.roadmap === true;
  const roadmapTarget = connector.roadmapTarget ?? null;
  const roadmapIsVersioned = Boolean(roadmapTarget && /\d/.test(roadmapTarget));
  const comingLabel = 'Coming soon';

  // Connecting / configuring / reconnecting are analyst+ writes (the connector
  // auth-url and token routes are analyst+). Viewers get a read-only hub: their
  // action button is disabled, EXCEPT the read-only "View data" action on an
  // already-connected system, which they keep.
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);

  // Token status on the SHARED cache, keyed per connector: it survives navigation
  // (no refetch when you come back to the hub), is deduped across tiles, and is
  // refreshed live — a connect/disconnect invalidates it, and another user's
  // change arrives via the org event stream. Disabled (null key) unless the
  // connector is actually connected, so we never ask for a token that cannot
  // exist. A failure is non-fatal: the tile just renders without the status.
  const { data: tokenStatusData } = useResource(
    isConnected && isEnabled ? cacheKeys.connectorTokenStatus(connector.id) : null,
    () => fetchTokenStatus(connector.id),
  );
  const tokenStatus: TokenStatus | null = tokenStatusData?.status ?? null;

  // R18-A3 T5 (AT-558): in a no-public-inbound deployment the browser
  // authorization-code flow can never complete (the provider redirect can't
  // reach the network). For a connector that has an outbound-only mode, we route
  // the customer to the outbound setup path (the modal, opened from this tile)
  // instead, so they can never start a flow that cannot complete (AC4).
  // Connectors with no outbound-only mode (GitHub, Slack) keep their button.
  const { hidesAuthorizationCodeConnect } = useNetworkProfileOptional();
  const hideAuthCode = hidesAuthorizationCodeConnect(connector.id);

  // "Token expired" is an authorization-code concept: a stored OAuth token that
  // lapsed and needs a browser reconnect. It is MEANINGLESS for an outbound-only
  // connector (JWT bearer / client-credentials), whose access token is minted on
  // demand — so token-status "needs_auth" there just means "not minted yet", not
  // "expired". Only surface the expiry badge / Reconnect for the auth-code posture.
  const tokenExpired =
    !hideAuthCode && (tokenStatus === 'needs_auth' || tokenStatus === 'refresh_failed');

  // Outbound connectors always route to the outbound setup path — the modal owns
  // enter / update / delete — whether or not they are already connected. Only for
  // a tile that is actually connectable (isEnabled); a gated-out connector (e.g.
  // Dynamics 365 / SAP) keeps its normal disabled "Connect" state.
  const outboundSetupGate = hideAuthCode && isEnabled;

  // When the token is expired/missing, override the button to "Reconnect".
  // A roadmap tile always shows its "Coming soon" label (it can never connect) —
  // that outranks every other posture, including multi-scope. Multi-scope cloud
  // connectors come next: their action opens the onboarding/health panel
  // ("Set up" when new, "Manage" once configured).
  const actionLabel = isRoadmap
    ? comingLabel
    : isMultiScope
    ? isConnected
      ? 'Manage'
      : 'Set up'
    : outboundSetupGate
    ? 'Set up outbound access'
    : tokenExpired
    ? 'Reconnect'
    : isConnected && !isConfigured
    ? 'Configure & Sync'
    : isConnected
    ? 'View data'
    : 'Connect';

  const actionVariant = isMultiScope
    ? isConnected
      ? 'secondary'
      : 'primary'
    : outboundSetupGate || !isConnected || tokenExpired
    ? 'primary'
    : isConfigured
    ? 'secondary'
    : 'tertiary';

  // R17-D4 Addendum A / T11: at the licensed limit, a NEW connection is blocked
  // (forward-only). Only applies to a not-yet-connected system — the 'connected'
  // tile actions (Configure & Sync / View data / Reconnect) are never gated, so
  // existing systems keep working when a lower-limit key lands (AC12).
  const limitBlocksNew = Boolean(connectBlocked) && !isConnected;
  // Viewers can only use the read-only "View data" action; every write action
  // (Connect / Configure & Sync / Reconnect) is disabled for them. A multi-scope
  // tile is exempt — it opens a read-only panel for Viewer/Analyst (health), with
  // Owner-only edits enforced inside the panel (MSP-B13 AT-748, T6-AC3).
  const viewerBlocks = isViewer && !isMultiScope && actionLabel !== 'View data';
  // A multi-scope tile only OPENS the panel (no OAuth/new connection here), so the
  // license new-connection gate never disables it. A roadmap tile is not
  // connectable at all, so it stays disabled regardless of posture.
  // A connect flow already in flight disables the action too, so the OAuth
  // round-trip can only ever be started once per click-through.
  const actionDisabled =
    isRoadmap || !isEnabled || (limitBlocksNew && !isMultiScope) || viewerBlocks || isConnecting;

  // R18-C0 P4 / AT-566: a connected tile offers Disconnect. Disconnecting is a
  // connector write (analyst+), so viewers never see it; it is independent of the
  // new-connection license gate (removing a connection is always allowed).
  const canDisconnect = isConnected && !isViewer && Boolean(onDisconnect);
  const disabledTitle = isRoadmap
    ? roadmapIsVersioned
      ? `${connector.name} is on the AgentIQ roadmap (coming in ${roadmapTarget}) and is not yet connectable.`
      : `${connector.name} is on the AgentIQ roadmap and is not yet connectable.`
    : (limitBlocksNew && !isMultiScope)
    ? (connectBlockMessage || 'Your license limit has been reached. Contact CloudFulcrum to add more.')
    : !isEnabled
    ? 'Connecting new sources is currently unavailable'
    : viewerBlocks
    ? 'Connecting systems requires an analyst or owner role.'
    : undefined;

  // Tooltip guiding the customer to the outbound setup path when the browser flow
  // is unavailable in this deployment (only when the button is otherwise enabled).
  const enabledTitle = !actionDisabled && outboundSetupGate
    ? 'This deployment has no public inbound — set up outbound-only access for this connector.'
    : undefined;

  return (
    <div
      onClick={onSelect}
      className={`connector-card flex h-[215px] cursor-pointer flex-col rounded-xl border ${
        selected ? 'connector-card-selected' : 'border-border bg-panel'
      } p-4 hover:border-accent/60 hover:bg-panel2`}
    >
      {/* Header */}
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2 text-base font-semibold text-text">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-accent/20 bg-accent/10 text-accent">
            {icon}
          </span>
          <span className="min-w-0 truncate leading-snug">{connector.name}</span>
        </div>
        <div className="mt-0.5 flex items-center justify-between gap-2">
          <div className="truncate text-xs text-muted">{connector.category}</div>
          <div className="flex shrink-0 items-center gap-1">
            {tokenExpired && (
              <span className="inline-flex items-center whitespace-nowrap rounded-full border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 text-xs font-medium leading-none text-amber-200">
                Token expired
              </span>
            )}
            {isRoadmap ? (
              // Roadmap tile: an honest Coming soon pill in place of the
              // meaningless connection-status badge (R191-R1 T5 / AT-726).
              <span
                data-testid="connector-roadmap-badge"
                className="integration-coming-soon-status-pill inline-flex items-center whitespace-nowrap rounded-full border text-xs font-medium leading-none"
              >
                {comingLabel}
              </span>
            ) : (
              <Badge status={badgeStatus} />
            )}
          </div>
        </div>
      </div>

      {/* Tags */}
      <div className="mt-2 flex min-w-0 flex-wrap items-start gap-1">
        {connector.reads.slice(0, 2).map((r) => (
          <span
            key={r}
            className="max-w-full truncate rounded-md border border-border bg-bg/30 px-2 py-0.5 text-[11px] text-muted"
          >
            {r}
          </span>
        ))}
      </div>

      {/* Signal */}
      <div className="mt-2 flex items-center justify-between text-xs text-muted">
        <span>
          Signal: <span className="text-text">{connector.signalStrength}</span>
        </span>
        <span>{isConfigured ? `Synced ${connector.lastSynced}` : '—'}</span>
      </div>

      {/* Button */}
      <div className="mt-auto flex items-center gap-2 pb-1 pt-4">
        <Button
          variant={actionVariant}
          disabled={actionDisabled}
          title={disabledTitle}
          ariaLabel={isConnecting ? `Connecting ${connector.name}` : undefined}
          className={`min-w-0 flex-1 ${
            isConnecting
              ? 'gap-1.5 !opacity-70'
              : actionDisabled
              ? '!bg-slate-500/10 !text-muted !border-border !opacity-100'
              : isConnected && isConfigured && !tokenExpired
              ? 'light-view-data-button !border-accent/50 !text-accent'
              : ''
          }`}
          onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
            e.stopPropagation();
            // Defensive: the button is already disabled while connecting, so a
            // second click can never reach the OAuth initiation.
            if (isConnecting) return;
            // MSP-B13 (AT-748): a multi-scope cloud connector onboards in the
            // detail panel — the tile action just selects it to open that panel
            // (never the OAuth Connect flow, which it does not use).
            if (isMultiScope) {
              onSelect();
              return;
            }
            // R18-A3 T5 (AT-558): in a no-public-inbound deployment, never start
            // the authorization-code flow for a connector with an outbound-only
            // mode — open the detail panel (outbound setup path) instead so the
            // customer can never begin a flow that cannot complete (AC4).
            if (outboundSetupGate) {
              // Pop the outbound/credential setup modal directly rather than
              // only re-selecting the tile (which looked like "nothing happens"
              // when the panel was already open). Fall back to selection when no
              // handler is wired.
              if (onSetupOutbound) onSetupOutbound();
              else onSelect();
              return;
            }
            // When the token needs a fresh OAuth round-trip, "Reconnect" must
            // start the OAuth flow — not fall through to the connected-tile
            // "View data" path on the page (CS-2 AC7).
            if (tokenExpired && onReconnect) {
              onReconnect();
            } else {
              onPrimary();
            }
          }}
        >
          {isConnecting ? (
            <>
              <Loader2 size={14} className="animate-spin" aria-hidden="true" />
              Connecting…
            </>
          ) : (
            actionLabel
          )}
        </Button>

        {canDisconnect && (
          <button
            type="button"
            title="Disconnect"
            aria-label={`Disconnect ${connector.name}`}
            onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
              e.stopPropagation();
              onDisconnect?.();
            }}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border text-muted transition-colors hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30"
          >
            <Unplug size={15} />
          </button>
        )}
      </div>
    </div>
  );
}
