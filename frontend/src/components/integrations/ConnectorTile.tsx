import React, { useEffect, useState } from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { fetchTokenStatus, TokenStatus } from '../../services/staticApi';
import { useAuthOptional } from '../../context/AuthContext';
import { useNetworkProfileOptional } from '../../context/NetworkProfileContext';
import { isViewerRole } from '../../utils/roles';

// Connectors whose Connect button is ENABLED on the Integration Hub. This is a
// UI gate only — the OAuth backends for the other connectors (Slack AT-420,
// Teams AT-434, Confluence/SharePoint AT-462, GitHub) remain fully wired
// (CONNECTOR_AUTH_CONFIGS + the generic auth-url → callback flow), so re-enabling
// one later is just adding its id back to this list.
//
// Product decision (July 2026): only the three systems of record are connectable
// from the hub for now; every other tile renders its action button disabled with
// the "Connecting new sources is currently unavailable" tooltip.
const ENABLED_CONNECTOR_IDS = [
  'salesforce', 'servicenow', 'jira',
];

export default function ConnectorTile({
  connector,
  icon,
  selected,
  onSelect,
  onPrimary,
  onReconnect,
  connectBlocked,
  connectBlockMessage,
}: {
  connector: Connector;
  icon: React.ReactNode;
  selected: boolean;
  onSelect: () => void;
  onPrimary: () => void;
  // Called when the token is expired/refresh-failed and the user clicks
  // "Reconnect". Must trigger the OAuth flow again (CS-2 AC7). Falls back to
  // onPrimary when not supplied so the tile keeps working in isolation.
  onReconnect?: () => void;
  // R17-D4 Addendum A / T11 (AT-506): when the org is at its licensed system
  // limit, disable the Connect action for a NEW (not-yet-connected) system and
  // show connectBlockMessage as its tooltip (AC10). Forward-only — an already
  // connected system keeps its Configure/View/Reconnect action, since
  // reconnecting is not a new connection and is never blocked.
  connectBlocked?: boolean;
  connectBlockMessage?: string;
}) {
  const isConnected = connector.status === 'connected';
  const isConfigured = connector.configured;
  const isEnabled = ENABLED_CONNECTOR_IDS.includes(connector.id);

  // Connecting / configuring / reconnecting are analyst+ writes (the connector
  // auth-url and token routes are analyst+). Viewers get a read-only hub: their
  // action button is disabled, EXCEPT the read-only "View data" action on an
  // already-connected system, which they keep.
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);

  const [tokenStatus, setTokenStatus] = useState<TokenStatus | null>(null);

  useEffect(() => {
    if (!isConnected || !isEnabled) {
      setTokenStatus(null);
      return;
    }
    let alive = true;
    fetchTokenStatus(connector.id)
      .then((r) => { if (alive) setTokenStatus(r.status); })
      .catch(() => { /* non-fatal — tile still renders without token status */ });
    return () => { alive = false; };
  }, [connector.id, isConnected, isEnabled]);

  // The token needs a fresh OAuth round-trip when there is no usable token
  // (needs_auth = missing/already-expired) or auto-refresh has given up
  // (refresh_failed). `connected` and `needs_refresh` are still usable, so no
  // Reconnect prompt. Values mirror the backend token-status contract (AC14).
  const tokenExpired = tokenStatus === 'needs_auth' || tokenStatus === 'refresh_failed';

  // R18-A3 T5 (AT-558): in a no-public-inbound deployment the browser
  // authorization-code flow can never complete (the provider redirect can't
  // reach the network). For a connector that has an outbound-only mode, we must
  // NOT offer the authorization-code Connect/Reconnect button — the customer is
  // routed to the outbound setup path (the connector detail panel) instead, so
  // they can never start a flow that cannot complete (AC4). Connectors with no
  // outbound-only mode (GitHub, Slack) keep their button and fall back to the
  // scoped-inbound package (R18-A3 T6).
  const { hidesAuthorizationCodeConnect } = useNetworkProfileOptional();
  const hideAuthCode = hidesAuthorizationCodeConnect(connector.id);
  // The only actions that START an authorization-code flow are Connect (new) and
  // Reconnect (expired token). Gating applies to those; connected-tile actions
  // (Configure & Sync / View data) never start an inbound-callback flow.
  // Only relabel a tile that is actually connectable (isEnabled) — a
  // not-yet-connectable connector (e.g. Dynamics 365 / SAP, gated out of the hub
  // by ENABLED_CONNECTOR_IDS) keeps its normal disabled "Connect" state rather
  // than showing a misleading, disabled "Set up outbound access" button.
  const wouldStartAuthCodeFlow = !isConnected || tokenExpired;
  const outboundSetupGate = hideAuthCode && wouldStartAuthCodeFlow && isEnabled;

  // When the token is expired/missing, override the button to "Reconnect"
  const actionLabel = outboundSetupGate
    ? 'Set up outbound access'
    : tokenExpired
    ? 'Reconnect'
    : isConnected && !isConfigured
    ? 'Configure & Sync'
    : isConnected
    ? 'View data'
    : 'Connect';

  const actionVariant = (!isConnected || tokenExpired) ? 'primary' : isConfigured ? 'secondary' : 'tertiary';

  // R17-D4 Addendum A / T11: at the licensed limit, a NEW connection is blocked
  // (forward-only). Only applies to a not-yet-connected system — the 'connected'
  // tile actions (Configure & Sync / View data / Reconnect) are never gated, so
  // existing systems keep working when a lower-limit key lands (AC12).
  const limitBlocksNew = Boolean(connectBlocked) && !isConnected;
  // Viewers can only use the read-only "View data" action; every write action
  // (Connect / Configure & Sync / Reconnect) is disabled for them.
  const viewerBlocks = isViewer && actionLabel !== 'View data';
  const actionDisabled = !isEnabled || limitBlocksNew || viewerBlocks;
  const disabledTitle = limitBlocksNew
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
            <Badge status={connector.status} />
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
      <div className="mt-auto pb-1 pt-4">
        <Button
          variant={actionVariant}
          disabled={actionDisabled}
          title={disabledTitle ?? enabledTitle}
          className={`w-full ${
            actionDisabled
              ? '!bg-slate-500/10 !text-muted !border-border !opacity-100'
              : isConnected && isConfigured && !tokenExpired
              ? 'light-view-data-button !border-accent/50 !text-accent'
              : ''
          }`}
          onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
            e.stopPropagation();
            // R18-A3 T5 (AT-558): in a no-public-inbound deployment, never start
            // the authorization-code flow for a connector with an outbound-only
            // mode — open the detail panel (outbound setup path) instead so the
            // customer can never begin a flow that cannot complete (AC4).
            if (outboundSetupGate) {
              onSelect();
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
          {actionLabel}
        </Button>
      </div>
    </div>
  );
}
