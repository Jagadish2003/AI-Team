import React from 'react';
import { ConnectorStatus } from '../../types/connector';

const map: Record<ConnectorStatus, { label: string; cls: string }> = {
  connected: { label: 'Connected', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
  not_connected: { label: 'Not connected', cls: 'bg-slate-500/10 text-muted border-border' },
  coming_soon: { label: 'Coming soon', cls: 'bg-amber-500/15 text-amber-200 border-amber-500/30' }
};

export default function Badge({ status }: { status: ConnectorStatus }) {
  const x = map[status];
  return <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${x.cls}`}>{x.label}</span>;
}
