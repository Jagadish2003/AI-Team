import React from 'react';
import { ConnectorStatus } from '../../types/connector';

const NOT_CONFIGURED = {
  label: 'Not configured',
  cls: 'integration-not-configured-status-pill',
};

const map: Record<ConnectorStatus, { label: string; cls: string }> = {
  connected: { label: 'Connected', cls: 'integration-connected-status-pill' },
  not_connected: { label: 'Not connected', cls: 'bg-slate-500/10 text-muted border-border' },
  disconnected: NOT_CONFIGURED,
  not_configured: NOT_CONFIGURED,
  // "Coming soon" was retired: a roadmap connector reads as "Not configured"
  // with a disabled Connect, never as a promise of a delivery date. Mapped here
  // at the SOURCE rather than only where callers remap, because the three
  // current consumers each normalise `coming_soon` to `not_configured`
  // themselves — a fourth that forgets would silently bring the old copy back.
  coming_soon: NOT_CONFIGURED,
};

export default function Badge({ status }: { status: ConnectorStatus }) {
  const x = map[status] || map.not_connected; 
  return <span className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium leading-none ${x.cls}`}>{x.label}</span>;
}
