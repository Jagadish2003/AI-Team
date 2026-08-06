import React from 'react';
import { ConnectorStatus } from '../../types/connector';

const map: Record<ConnectorStatus, { label: string; cls: string }> = {
  connected: { label: 'Connected', cls: 'integration-connected-status-pill' },
  not_connected: { label: 'Not connected', cls: 'bg-slate-500/10 text-muted border-border' },
  disconnected: { label: 'Not configured', cls: 'integration-not-configured-status-pill' },
  not_configured: { label: 'Not configured', cls: 'integration-not-configured-status-pill' },
  coming_soon: { label: 'Coming soon', cls: 'integration-coming-soon-status-pill' }
};

export default function Badge({ status }: { status: ConnectorStatus }) {
  const x = map[status] || map.not_connected; 
  return <span className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium leading-none ${x.cls}`}>{x.label}</span>;
}
