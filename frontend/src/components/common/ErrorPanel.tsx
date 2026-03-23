import React from 'react';
import Button from './Button';

export default function ErrorPanel({
  message,
  onRetry,
  title='Could not load data'
}: { message: string; onRetry?: ()=>void; title?: string }) {
  return (
    <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-6">
      <div className="text-sm font-semibold text-red-200">{title}</div>
      <div className="mt-1 text-xs text-red-100/80 whitespace-pre-wrap">{message}</div>
      {onRetry && (
        <div className="mt-4">
          <Button variant="secondary" onClick={onRetry}>Retry</Button>
        </div>
      )}
    </div>
  );
}
