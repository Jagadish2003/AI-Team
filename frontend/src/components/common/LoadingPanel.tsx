import React from 'react';

export default function LoadingPanel({
  title = 'Loading…',
  subtitle = 'Fetching data from the API.'
}: { title?: string; subtitle?: string }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-6">
      <div className="flex items-center gap-3">
        <div className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-accent" />
        <div>
          <div className="text-sm font-semibold text-text">{title}</div>
          <div className="mt-0.5 text-xs text-muted">{subtitle}</div>
        </div>
      </div>
    </div>
  );
}
