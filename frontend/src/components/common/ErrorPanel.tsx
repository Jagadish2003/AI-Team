import React from 'react';
import Button from './Button';

export default function ErrorPanel({
  message,
  onRetry,
  title = 'Could not load data'
}: { message: string; onRetry?: () => void; title?: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="w-80 max-w-sm rounded-xl border border-red-500/30 bg-red-500/10 px-12 py-8 flex flex-col items-center text-center gap-2">
        <div className="text-base font-semibold text-red-200">{title}</div>
        <div className="text-sm text-red-100/80 whitespace-pre-wrap">{message}</div>
        {onRetry && (
          <div className="mt-2">
            <Button variant="secondary" onClick={onRetry}>Retry</Button>
          </div>
        )}
      </div>
    </div>
  );
}