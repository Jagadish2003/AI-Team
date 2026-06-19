import React, { useEffect, useState } from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { fetchTokenStatus, TokenStatus } from '../../services/staticApi';

const ENABLED_CONNECTOR_IDS = ['salesforce', 'servicenow', 'jira'];

export default function ConnectorTile({
  connector,
  icon,
  selected,
  onSelect,
  onPrimary
}: {
  connector: Connector;
  icon: React.ReactNode;
  selected: boolean;
  onSelect: () => void;
  onPrimary: () => void;
}) {
  const isConnected = connector.status === 'connected';
  const isConfigured = connector.configured;
  const isEnabled = ENABLED_CONNECTOR_IDS.includes(connector.id);

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

  // When the token is expired/missing, override the button to "Reconnect"
  const actionLabel = tokenExpired
    ? 'Reconnect'
    : isConnected && !isConfigured
    ? 'Configure & Sync'
    : isConnected
    ? 'View data'
    : 'Connect';

  const actionVariant = (!isConnected || tokenExpired) ? 'primary' : isConfigured ? 'secondary' : 'tertiary';
  const actionDisabled = !isEnabled;

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
          title={actionDisabled ? 'Connecting new sources is currently unavailable' : undefined}
          className={`w-full ${
            actionDisabled
              ? '!bg-slate-500/10 !text-muted !border-border !opacity-100'
              : isConnected && isConfigured && !tokenExpired
              ? 'light-view-data-button !border-accent/50 !text-accent'
              : ''
          }`}
          onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
            e.stopPropagation();
            onPrimary();
          }}
        >
          {actionLabel}
        </Button>
      </div>
    </div>
  );
}
