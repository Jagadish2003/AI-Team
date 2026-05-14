/**
 * ConnectionStatusLegend — SB-1 Sprint 7
 *
 * Small legend shown below Group C on Screen 2.
 * Explains the three connection status dot colours.
 */

import React from 'react';

export default function ConnectionStatusLegend() {
  const items = [
    { color: 'bg-emerald-500', label: 'Connected' },
    { color: 'bg-amber-400',   label: 'Credentials needed' },
    { color: 'bg-slate-600',   label: 'Not yet configured' },
  ];

  return (
    <div className="flex items-center gap-5 mt-4 flex-wrap">
      {items.map(item => (
        <div key={item.label} className="flex items-center gap-1.5">
          <div className={`h-2 w-2 rounded-full ${item.color}`} aria-hidden="true" />
          <span className="text-xs text-muted">{item.label}</span>
        </div>
      ))}
      <div className="ml-auto">
        <span className="text-xs text-muted">
          Authentication for unconnected systems is managed in{' '}
          <span className="text-accent">Integration Hub</span>
        </span>
      </div>
    </div>
  );
}
