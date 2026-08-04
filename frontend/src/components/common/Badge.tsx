import React from 'react';
import { ConnectorStatus } from '../../types/connector';

// Every pill shares one shape — padding, radius, size and weight — so a tile's
// status pill does not change size as its status changes. `px-2.5 py-1` matches
// the 0.25rem/0.625rem the connected pill's colour class declares in styles.css,
// so the two agree rather than fighting in the cascade. The per-status classes
// below therefore carry COLOUR only.
const PILL =
  'inline-flex items-center whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-semibold leading-none';

const NEUTRAL = 'bg-slate-500/10 text-muted border-border';

const map: Record<ConnectorStatus, { label: string; cls: string }> = {
  connected: { label: 'Connected', cls: 'integration-connected-status-pill' },
  not_connected: { label: 'Not connected', cls: NEUTRAL },
  disconnected: { label: 'Disconnected', cls: NEUTRAL },
  not_configured: { label: 'Not configured', cls: NEUTRAL },
  coming_soon: { label: 'Coming soon', cls: 'integration-coming-soon-status-pill' }
};

export default function Badge({ status }: { status: ConnectorStatus }) {
  const x = map[status] || map.not_connected;
  return <span className={`${PILL} ${x.cls}`}>{x.label}</span>;
}
