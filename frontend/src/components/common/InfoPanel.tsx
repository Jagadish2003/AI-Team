import React from 'react';

export function InfoPanel({
  icon,
  iconClassName,
  title,
  message,
  actionLabel,
  onAction,
  actionDisabled = false,
  children,
}: {
  icon?: React.ReactNode;
  iconClassName?: string;
  title: string;
  message: React.ReactNode;
  actionLabel?: string;
  onAction?: () => void;
  actionDisabled?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex h-[75vh] items-center justify-center px-6">
      <div
        className="rounded-xl border border-border bg-panel p-8 py-12 text-center shadow-xl shadow-black/20"
        style={{ width: '840px', maxWidth: 'calc(100vw - 64px)' }}
      >
        {icon && (
          <div className="mb-4 flex justify-center">
            <div
              className={`flex h-14 w-14 items-center justify-center rounded-full border ${
                iconClassName ?? 'border-accent/20 bg-accent/10 text-accent'
              }`}
            >
              {icon}
            </div>
          </div>
        )}
        <h2 className="mb-4 text-xl font-semibold text-text">{title}</h2>
        <p className="mx-auto mb-6 max-w-[760px] text-sm leading-relaxed text-muted">{message}</p>
        {actionLabel && onAction && (
          <button
            onClick={onAction}
            disabled={actionDisabled}
            className="rounded-lg bg-accent px-6 py-2.5 text-sm font-medium text-textwhite transition-all duration-200 hover:scale-[1.02] hover:opacity-90 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:scale-100"
          >
            {actionLabel}
          </button>
        )}
        {children}
      </div>
    </div>
  );
}
