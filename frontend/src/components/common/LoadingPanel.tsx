import React from 'react';

export default function LoadingPanel({
  title = 'Loading…',
  subtitle = 'Fetching data from the API.',
}: {
  title?: string;
  subtitle?: string;
}) {
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="flex flex-col items-center gap-2">
        {/* Spinner */}
        <div className="relative mb-3">
          {/* Outer ring — static, faint */}
          <div className="h-10 w-10 rounded-full border-2 border-border opacity-30" />
          {/* Inner ring — spinning accent */}
          <div className="absolute inset-0 h-10 w-10 animate-spin rounded-full border-2 border-transparent border-t-accent" />
        </div>

        {/* Text */}
        <p className="text-sm font-semibold text-text">{title}</p>
        <p className="text-xs text-muted">{subtitle}</p>
      </div>
    </div>
  );
}