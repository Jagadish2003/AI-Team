import React from 'react';
import { AlertCircle } from 'lucide-react';
import Button from './Button';

/**
 * InlineError — a compact, theme-aware error notice for inline failures such as
 * a failed data fetch when the backend is unreachable. Unlike the full-height
 * ErrorPanel, this sits inside a step/section and reads cleanly in both light
 * and dark themes: it uses the shared surface tokens (bg-panel, border, text,
 * muted) with a subtle red accent, and the standard Button for Retry.
 */
export default function InlineError({
  message,
  title = 'Something went wrong',
  onRetry,
  retryLabel = 'Retry',
}: {
  message: string;
  title?: string;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-xl border border-red-500/30 bg-panel px-4 py-3.5 shadow-sm"
    >
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-red-500/10 text-red-400">
        <AlertCircle size={16} aria-hidden="true" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-semibold text-text">{title}</p>
        <p className="mt-0.5 text-sm leading-relaxed text-muted">{message}</p>
        {onRetry ? (
          <div className="mt-3">
            <Button variant="secondary" onClick={onRetry} className="px-4 py-1.5 text-sm">
              {retryLabel}
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
