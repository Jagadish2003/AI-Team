import React from 'react';

export default function LoadingPanel({
  title = 'Loading…',
  subtitle = 'Fetching data from the API.',
}: {
  title?: string;
  subtitle?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-20 px-4">
      {/* Spinner */}
      <div className="relative mb-5">
        {/* Outer ring — static, faint */}
        <div className="h-10 w-10 rounded-full border-2 border-border opacity-30" />
        {/* Inner ring — spinning accent */}
        <div className="absolute inset-0 h-10 w-10 animate-spin rounded-full border-2 border-transparent border-t-accent" />
      </div>

      {/* Text */}
      <p className="text-sm font-semibold text-text">{title}</p>
      <p className="mt-1 text-xs text-muted">{subtitle}</p>
    </div>
  );
}