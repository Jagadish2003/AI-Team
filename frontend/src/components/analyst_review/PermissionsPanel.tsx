import React from 'react';
import { OpportunityCandidate, PermissionItem } from '../../types/analystReview';

function rowStyle(p: PermissionItem) {
  if (p.satisfied) return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200';
  return p.required
    ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
    : 'border-border bg-bg/20 text-muted';
}

export default function PermissionsPanel({ opp }: { opp: OpportunityCandidate | null }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-semibold text-text">Required Data Permissions</div>
      {!opp ? (
        <div className="mt-3 text-sm text-muted">Select an opportunity to view permissions.</div>
      ) : (
        <>
          <div className="mt-3 space-y-2 text-sm">
            {opp.permissions.map((p, i) => (
              <div key={i} className={`rounded-md border px-3 py-2 ${rowStyle(p)}`}>
                <div className="flex items-center justify-between">
                  <span>{p.label}</span>
                  <span className="text-xs font-medium">
                    {p.satisfied ? '✓ Granted' : p.required ? '◇ Missing' : '○ Recommended'}
                  </span>
                </div>
              </div>
            ))}
          </div>
          <div className="mt-3 text-xs text-muted">
            Pilots often fail due to missing access. Confirm required scopes before committing timeline.
          </div>
        </>
      )}
    </div>
  );
}
