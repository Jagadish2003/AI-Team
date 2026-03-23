import React from 'react';
import { ReviewAuditEvent } from '../../types/analystReview';

export default function AuditTrail({ events }: { events: ReviewAuditEvent[] }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-semibold text-text">Audit Trail</div>
      <div className="mt-3 space-y-2">
        {events.slice(0, 10).map(e => (
          <div key={e.id} className="rounded-md border border-border bg-bg/20 px-3 py-2 text-sm">
            <div className="flex items-center justify-between text-xs text-muted">
              <span>{e.tsLabel}</span>
              <span>{e.by}</span>
            </div>
            <div className="mt-1 text-text">{e.action}</div>
          </div>
        ))}
        {events.length === 0 && (
          <div className="text-xs text-muted">No audit events recorded yet.</div>
        )}
      </div>
    </div>
  );
}
